"""Unit coverage for the PRO-02B density and exact-apply seams."""

from pathlib import Path
import importlib.util
import threading
import time
import types
import unittest


from test_align_similar_selected import STACK_TOOLS


TOPOLOGY = __import__("sys").modules["uv_gpt.topology_correspondence"]
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ISLAND_REFERENCE_PATH = PROJECT_ROOT / "uv_gpt" / "island_tools.py"
_ISLAND_REFERENCE_SPEC = importlib.util.spec_from_file_location(
    "uv_gpt_island_tools_reference",
    _ISLAND_REFERENCE_PATH,
)
ISLAND_REFERENCE = importlib.util.module_from_spec(_ISLAND_REFERENCE_SPEC)
assert _ISLAND_REFERENCE_SPEC.loader is not None
_ISLAND_REFERENCE_SPEC.loader.exec_module(ISLAND_REFERENCE)


class FakeUVData:
    def __init__(self, uv):
        self.uv = types.SimpleNamespace(
            x=float(uv[0]),
            y=float(uv[1]),
        )


class FakeLoop:
    def __init__(self, uv):
        self.data = FakeUVData(uv)

    def __getitem__(self, _layer):
        return self.data


class FakeMeshLoop:
    def __init__(self, uv, edge):
        self.data = FakeUVData(uv)
        self.edge = edge
        self.face = None
        self.link_loop_next = None

    def __getitem__(self, _layer):
        return self.data


class FakeMeshFace:
    def __init__(self, index, points, edges):
        self.index = index
        self.hide = False
        self.loops = [FakeMeshLoop(point, edge) for point, edge in zip(points, edges)]
        for loop in self.loops:
            loop.face = self
        for index, loop in enumerate(self.loops):
            loop.link_loop_next = self.loops[(index + 1) % len(self.loops)]


class FakeMesh:
    def __init__(self, faces):
        self.faces = list(faces)


class FakeGraphVertex:
    def __init__(self, index):
        self.index = index
        self.select = False


class FakeGraphEdge:
    def __init__(self, index):
        self.index = index
        self.link_faces = []
        self.select = False


class FakeGraphLoop:
    def __init__(self, vertex, edge, uv):
        self.vert = vertex
        self.edge = edge
        self.uv_data = FakeUVData(uv)
        self.face = None
        self.link_loop_next = None
        self.select = False

    def __getitem__(self, _layer):
        return self.uv_data


class FakeGraphFace:
    def __init__(self, index, loops):
        self.index = index
        self.loops = list(loops)
        self.hide = False
        self.select = False
        for loop in self.loops:
            loop.face = self
        for loop_index, loop in enumerate(self.loops):
            loop.link_loop_next = self.loops[(loop_index + 1) % len(self.loops)]


def _graph_triangle_island(face_index=0):
    vertices = [FakeGraphVertex(index) for index in range(3)]
    edges = [FakeGraphEdge(index) for index in range(3)]
    loops = [
        FakeGraphLoop(vertices[0], edges[0], (0.0, 0.0)),
        FakeGraphLoop(vertices[1], edges[1], (1.0, 0.0)),
        FakeGraphLoop(vertices[2], edges[2], (0.0, 1.0)),
    ]
    face = FakeGraphFace(face_index, loops)
    for edge in edges:
        edge.link_faces = [face]
    return tuple(loops)


def _enumeration_mesh():
    shared = object()
    face0 = FakeMeshFace(
        0,
        ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        (object(), shared, object()),
    )
    face1 = FakeMeshFace(
        1,
        ((0.0, 1.0), (1.0, 0.0), (1.0, 1.0)),
        (shared, object(), object()),
    )
    face2 = FakeMeshFace(
        2,
        ((3.0, 0.0), (4.0, 0.0), (3.0, 1.0)),
        (object(), object(), object()),
    )
    return FakeMesh((face0, face1, face2))


def _correspondence(mapping, accepted=True):
    return TOPOLOGY.CorrespondenceResult(
        accepted=accepted,
        loop_mapping=tuple(mapping),
        reason="accepted" if accepted else "topology_mismatch",
    )


def _triangle_graph(face_key=0, offset=(0.0, 0.0)):
    """Small valid immutable graph used only for worker-boundary tests."""

    points = (
        (0.0 + offset[0], 0.0 + offset[1]),
        (1.0 + offset[0], 0.0 + offset[1]),
        (0.0 + offset[0], 1.0 + offset[1]),
    )
    loop_keys = tuple((face_key, index) for index in range(3))
    loops = tuple(
        TOPOLOGY.LoopRecord(
            key=loop_keys[index],
            face_key=face_key,
            edge_key=index,
            vertex_key=index,
            next_key=loop_keys[(index + 1) % 3],
            prev_key=loop_keys[(index - 1) % 3],
            uv=points[index],
            boundary=True,
        )
        for index in range(3)
    )
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, loop_keys),),
        edges=tuple(
            TOPOLOGY.EdgeRecord(index, (loop_keys[index],), (face_key,), True)
            for index in range(3)
        ),
        vertices=tuple(
            TOPOLOGY.VertexRecord(index, (loop_keys[index],), True)
            for index in range(3)
        ),
        loops=loops,
        boundaries=(
            TOPOLOGY.BoundaryComponentRecord(
                "outer",
                loop_keys,
                role="outer",
            ),
        ),
    )


class AlignSimilarProTests(unittest.TestCase):
    def setUp(self):
        self._missing = object()
        self._original_settings = getattr(
            STACK_TOOLS.uv_utils, "get_settings", self._missing
        )
        STACK_TOOLS.uv_utils.get_settings = lambda _context: types.SimpleNamespace(
            stack_similarity_tolerance=0.01,
            stack_allow_flipping=True,
            stack_match_scale=True,
        )

    def tearDown(self):
        if self._original_settings is self._missing:
            del STACK_TOOLS.uv_utils.get_settings
        else:
            STACK_TOOLS.uv_utils.get_settings = self._original_settings

    @staticmethod
    def _session(evidence=None, **kwargs):
        return STACK_TOOLS._ProAlignSession(
            None,
            None,
            None,
            None,
            selected_islands=[],
            all_islands=[],
            evidence=evidence or {},
            **kwargs,
        )

    def test_density_uses_uv_over_world_area_and_rejects_invalid(self):
        self.assertAlmostEqual(
            STACK_TOOLS._pro_density_root_from_areas(4.0, 1.0),
            2.0,
        )
        self.assertAlmostEqual(
            STACK_TOOLS._pro_density_root_from_areas(4.0, 4.0),
            1.0,
        )
        for invalid in ((0.0, 1.0), (1.0, 0.0), (-1.0, 1.0), (1.0, float("nan"))):
            self.assertIsNone(STACK_TOOLS._pro_density_root_from_areas(*invalid))

    def test_density_master_is_independent_of_input_order_and_tie_breaks_by_key(self):
        records = [
            {"key": (9,), "density": 1.0},
            {"key": (2,), "density": 1.0 + 0.5e-12},
            {"key": (4,), "density": 1.5},
        ]
        self.assertEqual(
            STACK_TOOLS._pro_select_density_master(list(reversed(records)))["key"],
            (4,),
        )
        tie = [
            {"key": (9,), "density": 1.0},
            {"key": (2,), "density": 1.0 + 0.5e-12},
        ]
        self.assertEqual(STACK_TOOLS._pro_select_density_master(tie)["key"], (2,))

    def test_exact_staging_copies_master_loop_uv_and_requires_full_bijection(self):
        layer = object()
        master = {
            (0, 0): FakeLoop((1.0, 2.0)),
            (0, 1): FakeLoop((3.0, 4.0)),
        }
        candidate = {
            (7, 0): FakeLoop((9.0, 9.0)),
            (7, 1): FakeLoop((8.0, 8.0)),
        }
        result = _correspondence(
            [((7, 0), (0, 1)), ((7, 1), (0, 0))]
        )
        staged = STACK_TOOLS._pro_exact_write_values(master, candidate, result, layer)
        self.assertEqual(
            [item[2] for item in staged],
            [(3.0, 4.0), (1.0, 2.0)],
        )
        incomplete = _correspondence([((7, 0), (0, 1))])
        self.assertIsNone(
            STACK_TOOLS._pro_exact_write_values(master, candidate, incomplete, layer)
        )

    def test_exact_apply_changes_only_staged_candidate_loops(self):
        layer = object()
        master_loop = FakeLoop((1.0, 2.0))
        candidate_loop = FakeLoop((9.0, 9.0))
        bm = types.SimpleNamespace()
        obj = types.SimpleNamespace(data=object())
        original_update = getattr(STACK_TOOLS.bmesh, "update_edit_mesh", None)
        STACK_TOOLS.bmesh.update_edit_mesh = lambda *args, **kwargs: None
        try:
            count = STACK_TOOLS._pro_apply_staged_writes(
                obj,
                bm,
                layer,
                (((7, 0), candidate_loop, (1.0, 2.0)),),
                {(7, 0): (9.0, 9.0)},
            )
        finally:
            if original_update is None:
                del STACK_TOOLS.bmesh.update_edit_mesh
            else:
                STACK_TOOLS.bmesh.update_edit_mesh = original_update
        self.assertEqual(count, 1)
        candidate_uv = candidate_loop[layer].uv
        self.assertEqual(
            (candidate_uv.x, candidate_uv.y)
            if hasattr(candidate_uv, "x")
            else tuple(candidate_uv),
            (1.0, 2.0),
        )
        self.assertEqual(
            (master_loop[layer].uv.x, master_loop[layer].uv.y),
            (1.0, 2.0),
        )

    def test_rejected_topology_produces_no_staged_write(self):
        layer = object()
        master = {(0, 0): FakeLoop((1.0, 2.0))}
        candidate = {(7, 0): FakeLoop((9.0, 9.0))}
        before = (
            candidate[(7, 0)][layer].uv.x,
            candidate[(7, 0)][layer].uv.y,
        )
        result = _correspondence([((7, 0), (0, 0))], accepted=False)
        self.assertIsNone(
            STACK_TOOLS._pro_exact_write_values(master, candidate, result, layer)
        )
        self.assertEqual(
            (
                candidate[(7, 0)][layer].uv.x,
                candidate[(7, 0)][layer].uv.y,
            ),
            before,
        )

    def test_existing_operator_is_unchanged_and_two_pro_operators_are_separate(self):
        self.assertEqual(
            STACK_TOOLS.UVGPT_OT_align_to_selected.bl_idname,
            "uv_gpt.align_to_selected",
        )
        self.assertEqual(
            STACK_TOOLS.UVGPT_OT_align_similar_pro_fast.bl_idname,
            "uv_gpt.align_similar_pro_fast",
        )
        self.assertIn(
            STACK_TOOLS.UVGPT_OT_align_to_selected,
            STACK_TOOLS.classes,
        )
        self.assertIn(
            STACK_TOOLS.UVGPT_OT_align_similar_pro_fast,
            STACK_TOOLS.classes,
        )
        self.assertIn(
            STACK_TOOLS.UVGPT_OT_align_similar_pro_exact,
            STACK_TOOLS.classes,
        )
        ui_source = (PROJECT_ROOT / "uv_gpt" / "ui.py").read_text(encoding="utf-8")
        self.assertIn('row.operator("uv_gpt.align_to_selected"', ui_source)
        self.assertIn('uv_gpt.align_similar_pro_fast', ui_source)
        self.assertIn('uv_gpt.align_similar_pro_exact', ui_source)

    def test_shape_candidate_forwards_existing_controls(self):
        settings = types.SimpleNamespace(
            stack_match_scale=False,
            stack_allow_flipping=True,
            stack_similarity_tolerance=0.137,
        )
        captured = {}
        original_descriptor = STACK_TOOLS._descriptor_for_island
        original_match = STACK_TOOLS.similarity_matcher.match_descriptors

        def fake_descriptor(*args, **kwargs):
            return object()

        def fake_match(reference, candidate, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                accepted=True,
                score=0.0,
                transform=object(),
                diagnostics=STACK_TOOLS.similarity_matcher.get_diagnostics(),
            )

        STACK_TOOLS._descriptor_for_island = fake_descriptor
        STACK_TOOLS.similarity_matcher.match_descriptors = fake_match
        try:
            result = STACK_TOOLS._pro_shape_match_result(
                "master",
                "candidate",
                object(),
                settings,
                types.SimpleNamespace(),
                ("snapshot",),
                {},
            )
        finally:
            STACK_TOOLS._descriptor_for_island = original_descriptor
            STACK_TOOLS.similarity_matcher.match_descriptors = original_match
        self.assertTrue(result.accepted)
        self.assertFalse(captured["match_scale"])
        self.assertTrue(captured["allow_flipping"])
        self.assertEqual(captured["tolerance"], 0.137)
        self.assertTrue(captured["allow_tolerant_topology"])

    def test_pro_ownership_is_one_owner_star_without_chains(self):
        assigned = set()
        owners = set()
        self.assertTrue(
            STACK_TOOLS._pro_ownership_allows("member", "master", assigned, owners)
        )
        STACK_TOOLS._pro_commit_ownership("member", "master", assigned, owners)
        self.assertFalse(
            STACK_TOOLS._pro_ownership_allows("member", "other", assigned, owners)
        )
        self.assertFalse(
            STACK_TOOLS._pro_ownership_allows("master", "other", assigned, owners)
        )
        self.assertFalse(
            STACK_TOOLS._pro_ownership_allows("other", "member", assigned, owners)
        )

    def test_pro_master_order_keeps_density_owner_and_face_key_ties(self):
        densities = {"high": 2.0, "low": 1.0, "a": 1.0, "b": 1.0}
        self.assertTrue(STACK_TOOLS._pro_master_precedes("high", "low", densities))
        self.assertFalse(STACK_TOOLS._pro_master_precedes("low", "high", densities))
        self.assertTrue(STACK_TOOLS._pro_master_precedes("a", "b", densities))
        self.assertFalse(STACK_TOOLS._pro_master_precedes("b", "a", densities))

    def test_pro_graph_cache_is_lazy_bounded_and_releases_old_entries(self):
        cache = STACK_TOOLS._ProGraphLRU(limit=2)
        builds = []

        def build(key):
            return lambda: builds.append(key) or (key,)

        self.assertEqual(cache.get_or_build("a", build("a")), ("a",))
        self.assertEqual(cache.get_or_build("a", build("a-again")), ("a",))
        self.assertEqual(cache.get_or_build("b", build("b")), ("b",))
        self.assertEqual(cache.get_or_build("c", build("c")), ("c",))
        self.assertEqual(builds, ["a", "b", "c"])
        self.assertEqual(cache.builds, 3)
        self.assertEqual(cache.hits, 1)
        self.assertEqual(cache.peak, 2)

    def test_pro_graph_builder_matches_synchronous_graph_oracle(self):
        layer = object()
        island = _graph_triangle_island()
        expected_graph, expected_loops = STACK_TOOLS._pro_graph_for_island(
            island,
            layer,
        )
        state = STACK_TOOLS._ProGraphBuildState(island, layer)
        actual_graph, actual_loops = state.run_to_completion(operation_budget=1)

        self.assertEqual(actual_graph, expected_graph)
        self.assertEqual(tuple(actual_loops), tuple(expected_loops))
        self.assertEqual(
            tuple(actual_loops[key] for key in actual_loops),
            tuple(expected_loops[key] for key in expected_loops),
        )
        self.assertEqual(state.phase, "done")
        self.assertGreater(state.graph_slices, 1)
        self.assertGreater(state.graph_primitive_operations, 1)
        self.assertTrue(state.phase_transitions)

    def test_pro_graph_builder_pause_resume_obeys_operation_cap(self):
        layer = object()
        state = STACK_TOOLS._ProGraphBuildState(_graph_triangle_island(), layer)
        result, operations = state.advance(operation_budget=2)
        self.assertIsNone(result)
        self.assertEqual(operations, 2)

        slices = 1
        while not state.done:
            result, operations = state.advance(operation_budget=3)
            self.assertLessEqual(operations, 3)
            slices += 1
            self.assertLess(slices, 1000)
        self.assertIsNotNone(result)
        self.assertEqual(state.graph_slices, slices)
        self.assertGreaterEqual(state.max_graph_slice_ms, 0.0)

    def test_pro_graph_builder_cancel_discards_partial_session_state(self):
        layer = object()
        state = STACK_TOOLS._ProGraphBuildState(_graph_triangle_island(), layer)
        state.advance(operation_budget=2)
        session = STACK_TOOLS._ProAlignSession(
            None,
            None,
            None,
            layer,
            selected_islands=[],
            all_islands=[],
            evidence={},
        )
        session._graph_build_state = state
        session._graph_build_key = (0,)
        session._pending_graph_pair = ((0,), (1,))
        session._pending_graph_pair_object = types.SimpleNamespace(
            master_key=(0,),
            member_key=(1,),
        )
        session._staged_writes = [("target", "loop", (1.0, 2.0))]

        session.cancel("user_cancelled")

        self.assertTrue(session.done)
        self.assertIsNone(session._graph_build_state)
        self.assertIsNone(session._pending_graph_pair)
        self.assertEqual(session._staged_writes, [])
        self.assertEqual(session.report["exact_loop_writes"], 0)

    def test_pro_graph_cache_stores_only_completed_resumable_graph(self):
        layer = object()
        island = _graph_triangle_island()
        session = STACK_TOOLS._ProAlignSession(
            None,
            None,
            None,
            layer,
            selected_islands=[],
            all_islands=[],
            evidence={},
        )
        key = (0,)
        session._key_to_island[key] = island

        status, graph, loops, error = session._advance_graph_for(
            key,
            deadline=time.perf_counter(),
        )
        self.assertEqual(status, "pending")
        self.assertIsNone(graph)
        self.assertIsNone(loops)
        self.assertIsNone(error)
        self.assertEqual(session._graph_cache.builds, 0)

        while status == "pending":
            status, graph, loops, error = session._advance_graph_for(key)
        self.assertEqual(status, "ready")
        self.assertIsNotNone(graph)
        self.assertIsNotNone(loops)
        self.assertIsNone(error)
        self.assertEqual(session._graph_cache.builds, 1)
        self.assertIsNone(session._graph_build_state)

        status, cached_graph, cached_loops, error = session._advance_graph_for(key)
        self.assertEqual(status, "ready")
        self.assertIs(cached_graph, graph)
        self.assertIs(cached_loops, loops)
        self.assertIsNone(error)
        self.assertEqual(session._graph_cache.builds, 1)
        self.assertEqual(session._graph_cache.hits, 1)

    def test_pro_wall_budget_cutoff_is_compact_and_explicit(self):
        report = {"aligned_exact": 2, "truncation_reasons": []}
        self.assertTrue(
            STACK_TOOLS._pro_budget_cutoff(
                report,
                STACK_TOOLS.time.perf_counter() - 1.0,
                "correspondence",
            )
        )
        self.assertTrue(report["truncated"])
        self.assertTrue(report["partial"])
        self.assertEqual(report["budget_cutoff_stage"], "correspondence")
        self.assertEqual(report["truncation_reasons"], ["wall_time_budget"])

    def test_pro_normal_evidence_does_not_include_mapping_pairs(self):
        sentinel = object()
        original_settings = getattr(STACK_TOOLS.uv_utils, "get_settings", sentinel)
        STACK_TOOLS.uv_utils.get_settings = lambda _context: types.SimpleNamespace(
            stack_similarity_tolerance=0.01,
        )
        report = {}
        try:
            result = STACK_TOOLS._align_selected_similar_pro(
                None,
                None,
                None,
                None,
                [],
                evidence=report,
                all_islands=[],
            )
        finally:
            if original_settings is sentinel:
                del STACK_TOOLS.uv_utils.get_settings
            else:
                STACK_TOOLS.uv_utils.get_settings = original_settings
        self.assertEqual(result, (0, 0))
        self.assertEqual(report["groups"], [])
        self.assertNotIn("mapping_pairs", report)

    def test_pro_session_background_and_modal_share_one_completion_state_machine(self):
        session = self._session()
        result = session.run_to_completion()

        self.assertEqual(result, (0, 0))
        self.assertTrue(session.done)
        self.assertEqual(session.state, "done")
        self.assertEqual(session.report["session_state"], "done")
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertEqual(session.report["tick_count"], 1)
        self.assertIsNone(session.selected_islands)
        self.assertIsNone(session.all_islands)

    def test_pro_island_enumerator_batches_and_matches_existing_membership_order(self):
        mesh = _enumeration_mesh()
        layer = object()
        expected = ISLAND_REFERENCE.get_uv_islands(
            mesh,
            layer,
            selected_only=False,
        )
        state = STACK_TOOLS._ProIslandEnumerationState(mesh, layer)
        first_result, first_operations = state.advance(
            operation_budget=3,
            deadline=time.perf_counter() + 1.0,
        )
        self.assertIsNone(first_result)
        self.assertEqual(first_operations, 3)
        slices = 1
        while not state.done:
            result, operations = state.advance(
                operation_budget=7,
                deadline=time.perf_counter() + 1.0,
            )
            self.assertLessEqual(operations, 7)
            slices += 1
            self.assertLess(slices, 100)
        self.assertEqual(
            tuple(tuple(loop.face.index for loop in island) for island in state.result),
            tuple(tuple(loop.face.index for loop in island) for island in expected),
        )
        self.assertGreater(state.enum_primitive_operations, state.enum_slices)
        self.assertEqual(state.enum_slices, slices)
        self.assertGreaterEqual(state.max_enum_slice_ms, 0.0)
        self.assertTrue(state.phase_transitions)
        self.assertEqual(state.phase, "done")

    def test_pro_cancel_during_batched_enumeration_has_zero_writes(self):
        mesh = _enumeration_mesh()
        session = STACK_TOOLS._ProAlignSession(
            None,
            None,
            mesh,
            object(),
            selected_islands=None,
            all_islands=None,
            evidence={},
        )
        session.step(active_budget_ms=0.0)
        self.assertFalse(session.done)
        self.assertEqual(session.state, "prepare")
        self.assertGreaterEqual(session.report["enum_slices"], 1)
        session.cancel("user_cancelled")
        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertEqual(session.report["aligned_exact"], 0)
        self.assertEqual(session._staged_writes, [])

    def test_pro_cancel_before_work_is_zero_write_and_cleans_state(self):
        session = self._session()
        session.cancel("user_cancelled")

        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.report["cancel_reason"], "user_cancelled")
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertEqual(session.report["aligned_exact"], 0)
        self.assertEqual(session._staged_writes, [])

    def test_pro_cancel_after_staging_discards_everything_without_apply(self):
        session = self._session()
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.report["aligned_exact"] = 1
        original_apply = STACK_TOOLS._pro_apply_staged_writes
        STACK_TOOLS._pro_apply_staged_writes = lambda *args: self.fail(
            "cancel must not apply staged writes"
        )
        try:
            session.cancel("user_cancelled")
        finally:
            STACK_TOOLS._pro_apply_staged_writes = original_apply

        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.report["staged_exact_before_cancel"], 1)
        self.assertEqual(session.report["aligned_exact"], 0)
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertEqual(session._staged_writes, [])

    def test_pro_modal_slow_cancel_is_nonblocking_and_zero_write(self):
        """A slow owned helper cannot delay modal cancel or retain staged UVs."""

        session = self._session(
            modal=True,
            process_worker_count=1,
            process_batch_size=1,
            process_test_override=True,
            process_fused=True,
        )
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.report["aligned_exact"] = 1
        selection_snapshot = object()
        active_snapshot = object()
        session._selection_snapshot = selection_snapshot
        session._active_snapshot = active_snapshot

        class SlowBlockingPool:
            cancel_called = False
            begin_calls = 0
            advance_calls = 0
            cancel_complete = False
            shutdown_complete = False
            shutdown_timings_ms = ()
            shutdown_state = "cancelled"
            shutdown_rounds = 0
            shutdown_force_used = True
            job_object_capability = types.SimpleNamespace(
                requested=False,
                available=False,
                kill_on_close=False,
                reason="fake",
            )

            def cancel(self, **_kwargs):
                self.cancel_called = True
                time.sleep(0.6)
                raise AssertionError("modal cancel used the blocking pool API")

            def begin_cancel(self):
                self.begin_calls += 1
                return "force"

            def advance_cancel(self, **_kwargs):
                self.advance_calls += 1
                self.cancel_complete = True
                self.shutdown_complete = True
                return "complete"

        pool = SlowBlockingPool()

        class FakePipeline:
            def __init__(self):
                self.stage = "exact_wait"
                self.pool = pool

            @property
            def is_terminal(self):
                return self.stage in {"cancelled", "failed"}

            def cancel(self, *, nonblocking=False, **_kwargs):
                self.assert_nonblocking = bool(nonblocking)
                self.pool.begin_cancel()
                self.stage = "cancelling"
                return None

            def advance_cancel(self, **_kwargs):
                self.pool.advance_cancel()
                if self.pool.cancel_complete:
                    self.stage = "cancelled"
                return None

        pipeline = FakePipeline()
        session._process_pool = pool
        session._process_pipeline = pipeline
        finalized = []
        original_finalize = session._finalize
        session._finalize = lambda *, apply: finalized.append(bool(apply)) or setattr(
            session, "done", True
        )
        try:
            started = time.perf_counter()
            session.cancel("user_cancelled", nonblocking=True)
            cancel_ms = (time.perf_counter() - started) * 1000.0
            self.assertLessEqual(cancel_ms, 250.0)
            self.assertFalse(pool.cancel_called)
            self.assertEqual(pool.begin_calls, 1)
            self.assertEqual(session.state, "process_cancel")
            self.assertEqual(session._staged_writes, [])
            self.assertEqual(session.report["exact_loop_writes"], 0)
            self.assertIs(session._selection_snapshot, selection_snapshot)
            self.assertIs(session._active_snapshot, active_snapshot)

            started = time.perf_counter()
            session._advance_process_cancel()
            advance_ms = (time.perf_counter() - started) * 1000.0
            self.assertLessEqual(advance_ms, 250.0)
            self.assertEqual(finalized, [False])
            self.assertTrue(session.done)
            self.assertEqual(session.report["process_cancel_state"], "complete")
        finally:
            session._finalize = original_finalize

    def test_pro_timeout_keeps_fully_staged_results_and_records_partial_reason(self):
        session = self._session()
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.report["aligned_exact"] = 1
        calls = []
        original_apply = STACK_TOOLS._pro_apply_staged_writes
        STACK_TOOLS._pro_apply_staged_writes = (
            lambda *args: calls.append(args) or 1
        )
        try:
            session._request_timeout("candidate_pair")
            session._finalize(apply=True)
        finally:
            STACK_TOOLS._pro_apply_staged_writes = original_apply

        self.assertTrue(session.done)
        self.assertTrue(session.report["truncated"])
        self.assertTrue(session.report["partial"])
        self.assertEqual(session.report["truncation_reasons"], ["wall_time_budget"])
        self.assertEqual(session.report["budget_cutoff_stage"], "candidate_pair")
        self.assertEqual(session.report["exact_loop_writes"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(session._staged_writes, [])

    def test_process_timeout_discards_incomplete_staged_prefix(self):
        session = self._session(
            process_worker_count=1,
            process_batch_size=16,
            process_test_override=True,
            process_fused=True,
        )
        # No canonical process result exists; this models the R2F2
        # 458-submitted/457-completed/435-merged incomplete finish seam.
        session._process_pipeline = None
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.report["aligned_exact"] = 1
        original_apply = STACK_TOOLS._pro_apply_staged_writes
        STACK_TOOLS._pro_apply_staged_writes = lambda *args: self.fail(
            "incomplete process result must never apply staged writes"
        )
        try:
            session._request_timeout("process_pipeline")
            session._finalize(apply=True)
        finally:
            STACK_TOOLS._pro_apply_staged_writes = original_apply
        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertTrue(session.report["truncated"])
        self.assertFalse(session.report["partial"])
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertEqual(session._staged_writes, [])

    def test_pro_context_invalidation_cancels_without_apply(self):
        session = self._session()
        original_valid = STACK_TOOLS._pro_session_context_valid
        original_apply = STACK_TOOLS._pro_apply_staged_writes
        STACK_TOOLS._pro_session_context_valid = lambda *args: False
        STACK_TOOLS._pro_apply_staged_writes = lambda *args: self.fail(
            "invalid context must not apply"
        )
        try:
            session.context = object()
            session.obj = object()
            session.bm = object()
            session.uv_layer = object()
            session.step()
        finally:
            STACK_TOOLS._pro_session_context_valid = original_valid
            STACK_TOOLS._pro_apply_staged_writes = original_apply

        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.report["cancel_reason"], "context_invalidated")
        self.assertEqual(session.report["exact_loop_writes"], 0)

    def test_pro_reentry_guard_rejects_second_session(self):
        original_active = STACK_TOOLS._ACTIVE_PRO_SESSION
        STACK_TOOLS._ACTIVE_PRO_SESSION = types.SimpleNamespace(done=False)
        try:
            with self.assertRaisesRegex(RuntimeError, "already active"):
                STACK_TOOLS._pro_create_session(None)
        finally:
            STACK_TOOLS._ACTIVE_PRO_SESSION = original_active

    def test_pro_default_and_override_search_budgets_are_explicit(self):
        default = self._session()
        override = self._session(correspondence_max_search=512)

        self.assertEqual(default.correspondence_max_search, 1024)
        self.assertEqual(default.report["correspondence_max_search"], 1024)
        self.assertEqual(override.correspondence_max_search, 512)
        self.assertEqual(override.report["correspondence_max_search"], 512)

    def test_pro_exact_correspondence_forwards_the_session_search_budget(self):
        session = self._session(correspondence_max_search=512)
        master_key, member_key = (1,), (2,)
        session._density_by_key.update({master_key: 2.0, member_key: 1.0})
        session._cheap_signatures.update({master_key: object(), member_key: object()})
        session._key_to_island.update({master_key: object(), member_key: object()})
        session._settings = types.SimpleNamespace(
            stack_allow_flipping=True,
            stack_match_scale=True,
            stack_similarity_tolerance=0.01,
        )
        session.report["shape_fit_accepted"] = 0
        session._descriptor_cache = object()
        session._snapshot_identity = ("snapshot",)
        session._graph_for = lambda _key: (object(), object(), None)
        original_boundary = STACK_TOOLS.similarity_matcher.cheap_boundary_gate
        original_topology = STACK_TOOLS.similarity_matcher.cheap_topology_gate
        original_shape = STACK_TOOLS._pro_shape_match_result
        original_exact_write = STACK_TOOLS._pro_exact_write_values
        captured = {}

        class FakeWorker:
            def submit(self, master_graph, candidate_graph, **kwargs):
                captured["master_graph"] = master_graph
                captured["candidate_graph"] = candidate_graph
                captured.update(kwargs)
                return 1

        STACK_TOOLS.similarity_matcher.cheap_boundary_gate = (
            lambda *_args: types.SimpleNamespace(passed=True)
        )
        STACK_TOOLS.similarity_matcher.cheap_topology_gate = (
            lambda *_args: types.SimpleNamespace(passed=True)
        )
        STACK_TOOLS._pro_shape_match_result = lambda *args: types.SimpleNamespace(
            accepted=True,
            score=0.0,
            transform=object(),
        )
        STACK_TOOLS._pro_exact_write_values = lambda *args: []
        session._worker = FakeWorker()
        try:
            accepted = session._process_pair(
                types.SimpleNamespace(master_key=master_key, member_key=member_key)
            )
        finally:
            STACK_TOOLS.similarity_matcher.cheap_boundary_gate = original_boundary
            STACK_TOOLS.similarity_matcher.cheap_topology_gate = original_topology
            STACK_TOOLS._pro_shape_match_result = original_shape
            STACK_TOOLS._pro_exact_write_values = original_exact_write

        self.assertTrue(accepted)
        self.assertEqual(captured["max_search"], 512)
        self.assertEqual(session.report["correspondence_calls"], 1)
        self.assertEqual(session.report["aligned_exact"], 0)
        self.assertIsNotNone(session._inflight)

    def test_pro_live_exact_path_uses_one_resumable_search_without_worker(self):
        session = self._session(correspondence_max_search=1024)
        master_key, member_key = (1,), (2,)
        layer = object()
        master_loops = {
            (0, index): FakeLoop(point)
            for index, point in enumerate(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)))
        }
        candidate_loops = {
            (7, index): FakeLoop(point)
            for index, point in enumerate(((9.0, 9.0), (10.0, 9.0), (9.0, 10.0)))
        }
        session._density_by_key.update({master_key: 2.0, member_key: 1.0})
        session._cheap_signatures.update({master_key: object(), member_key: object()})
        session._key_to_island.update({master_key: object(), member_key: object()})
        session._settings = types.SimpleNamespace(
            stack_allow_flipping=True,
            stack_match_scale=True,
            stack_similarity_tolerance=0.01,
        )
        session.report["shape_fit_accepted"] = 0
        session.uv_layer = layer
        session._graph_for = lambda key: (
            _triangle_graph(0 if key == master_key else 7),
            master_loops if key == master_key else candidate_loops,
            None,
        )
        original_boundary = STACK_TOOLS.similarity_matcher.cheap_boundary_gate
        original_topology = STACK_TOOLS.similarity_matcher.cheap_topology_gate
        original_shape = STACK_TOOLS._pro_shape_match_result
        STACK_TOOLS.similarity_matcher.cheap_boundary_gate = (
            lambda *_args: types.SimpleNamespace(passed=True)
        )
        STACK_TOOLS.similarity_matcher.cheap_topology_gate = (
            lambda *_args: types.SimpleNamespace(passed=True)
        )
        STACK_TOOLS._pro_shape_match_result = lambda *args: types.SimpleNamespace(
            accepted=True,
            score=0.0,
            transform=object(),
        )
        try:
            self.assertIsNone(session._worker)
            self.assertTrue(
                session._process_pair(
                    types.SimpleNamespace(master_key=master_key, member_key=member_key)
                )
            )
            inflight = session._inflight
            self.assertIsNotNone(inflight)
            self.assertIsInstance(
                inflight["search"], TOPOLOGY.CorrespondenceSearch
            )
            self.assertEqual(inflight["search"].max_search, 1024)
            exact_step = session._advance_resumable_search(
                time.perf_counter() + 1.0
            )
            self.assertIsNotNone(exact_step)
            self.assertIn(exact_step.status, {"success", "failure"})
            session._consume_resumable_step(exact_step)
            self.assertEqual(session.report["aligned_exact"], 1)
            self.assertEqual(session.report["worker_submissions"], 0)
            self.assertGreaterEqual(session.report["exact_search_slices"], 1)
            self.assertEqual(len(session._staged_writes), 3)
            session.cancel("test_cleanup")
            self.assertEqual(session._staged_writes, [])
        finally:
            STACK_TOOLS.similarity_matcher.cheap_boundary_gate = original_boundary
            STACK_TOOLS.similarity_matcher.cheap_topology_gate = original_topology
            STACK_TOOLS._pro_shape_match_result = original_shape
            if not session.done:
                session.cancel("test_cleanup")

    def test_mc3a_process_mode_is_explicit_and_locked_to_one_worker_one_batch(self):
        session = self._session(process_worker_count=1, process_batch_size=1)
        self.assertTrue(session.process_requested)
        self.assertEqual(session.process_worker_count, 1)
        self.assertEqual(session.process_batch_size, 1)
        with self.assertRaises(ValueError):
            self._session(process_worker_count=2, process_batch_size=1)
        with self.assertRaises(ValueError):
            self._session(process_worker_count=1, process_batch_size=2)

    def test_r1f_fused_mode_is_opt_in_and_keeps_external_pipeline_separate(self):
        fused = self._session(
            process_worker_count=1,
            process_batch_size=16,
            process_test_override=True,
            process_fused=True,
        )
        self.assertTrue(fused.process_requested)
        self.assertTrue(fused.process_pipeline_requested)
        self.assertTrue(fused.process_fused_requested)
        self.assertTrue(fused.report["process_fused"])
        with self.assertRaises(ValueError):
            self._session(process_fused=True)

    def test_r1g_fused_context_ack_latch_survives_pool_cleanup_and_rejects_stale(self):
        session = self._session(
            process_worker_count=1,
            process_batch_size=16,
            process_test_override=True,
            process_fused=True,
        )
        identity = STACK_TOOLS.pro_process_payload.SnapshotIdentity(
            "r1g-ack", 0, "snapshot-r1g"
        )
        session._process_identity = identity
        session._process_graph_context = types.SimpleNamespace(
            identity=identity,
            fused_digest="fused-r1g",
        )
        pool = types.SimpleNamespace(
            context_load_acked=0,
            graph_context_ready=False,
        )

        self.assertFalse(session._latch_process_fused_context_ack(pool))
        self.assertFalse(session._process_fused_context_acked)

        pool.context_load_acked = 1
        pool.graph_context_ready = True
        self.assertTrue(session._latch_process_fused_context_ack(pool))

        # Pool cleanup removes its instantaneous ready state, but the session
        # still reports the valid ACK observed for its immutable generation.
        pool.context_load_acked = 0
        pool.graph_context_ready = False
        session._process_pool = None
        session._process_last_progress = None
        session._update_worker_report()
        self.assertTrue(session._process_fused_context_acked)
        self.assertTrue(session.report["process_fused_context_ready"])

        # A generation mismatch invalidates the historical ACK and cannot
        # relatch from the old context.
        session._process_generation = 1
        self.assertFalse(session._latch_process_fused_context_ack(pool))
        self.assertFalse(session._process_fused_context_acked)

    def test_r1i_context_load_true_admits_fused_pipeline_before_shutdown(self):
        session = self._session(
            process_worker_count=1,
            process_batch_size=16,
            process_test_override=True,
            process_fused=True,
        )
        identity = STACK_TOOLS.pro_process_payload.SnapshotIdentity(
            "r1i-admission", 0, "snapshot-r1i"
        )
        session._process_identity = identity
        session._process_graph_context = types.SimpleNamespace(
            identity=identity,
            fused_digest="fused-r1i",
        )
        pool = types.SimpleNamespace(
            load_graph_context=lambda _context, deadline=None: True,
        )

        context_ready = pool.load_graph_context(session._process_graph_context)
        self.assertTrue(context_ready)
        self.assertTrue(session._admit_process_fused_context(context_ready))
        session._process_pipeline = object()
        self.assertTrue(session._process_fused_context_acked)

        # The pipeline has already been admitted; final publication must keep
        # the historical ACK after the pool itself is released.
        session._process_pool = None
        session._process_last_progress = None
        session._update_worker_report()
        self.assertTrue(session.report["process_fused_context_ready"])

    def test_mc3a_process_poll_is_zero_wait_and_converts_rejected_result(self):
        adapter = STACK_TOOLS.pro_process_adapter
        payload = __import__("sys").modules["uv_gpt.pro_process_payload"]
        options = adapter.make_exact_options(
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-6,
            max_search=32,
        )
        identity = adapter.capture_snapshot(
            None,
            None,
            None,
            None,
            [],
            session_nonce="mc3a-unit",
            generation=0,
            options=options,
        ).identity
        task = adapter.make_single_pair_batch(
            identity,
            pair_ordinal=0,
            master_key=(0,),
            member_key=(1,),
            master_graph=_triangle_graph(0),
            member_graph=_triangle_graph(1),
            options=options,
        )
        pair = task.pair_tasks[0]
        pair_result = payload.PairResult(
            pair_ordinal=0,
            master_key=pair.master_key,
            member_key=pair.member_key,
            master_graph_digest=pair.master_graph.content_digest,
            member_graph_digest=pair.member_graph.content_digest,
            accepted=False,
            reason="topology_mismatch",
        )

        class FakePool:
            is_terminal = True
            worker_pids = ()

            def __init__(self):
                self.poll_timeouts = []

            def poll(self, timeout=0.0):
                self.poll_timeouts.append(timeout)
                return types.SimpleNamespace(active_workers=0, retry_count=0)

            def final_result(self):
                return types.SimpleNamespace(
                    complete=True,
                    results=(pair_result,),
                    result_digest="unit-result",
                    failure="",
                )

        session = self._session(process_worker_count=1, process_batch_size=1)
        session._process_pool = FakePool()
        session._process_session_nonce = "mc3a-unit"
        session._process_identity = identity
        session._process_options = options
        session._inflight = {
            "process": True,
            "token": task.batch_id,
            "task": task,
            "master_key": pair.master_key,
            "member_key": pair.member_key,
            "master_loops": {},
            "candidate_loops": {},
            "submitted_at": time.perf_counter(),
        }
        result = session._poll_process_worker()
        self.assertIsNotNone(result)
        self.assertFalse(result.accepted)
        self.assertEqual(session._process_pool.poll_timeouts, [0.0])
        self.assertTrue(
            session._consume_exact_result(result, token=task.batch_id, error=None)
        )
        self.assertEqual(session.report["aligned_exact"], 0)
        session._process_pool = None
        session.cancel("test_cleanup")

    def test_pro_incremental_plan_builder_matches_candidate_plan_stream(self):
        records = [
            STACK_TOOLS.pro_candidate_planner.IslandRecord(
                (index,),
                ("strict", 3),
                (0.20 + index * 0.01, 0.40),
                1.0 + index,
                ("cheap", index % 2),
            )
            for index in range(6)
        ]
        config = STACK_TOOLS._PRO_CANDIDATE_PLANNER_CONFIG
        expected = STACK_TOOLS.pro_candidate_planner.plan_candidates(
            records,
            config,
        )
        builder = STACK_TOOLS._ProIncrementalPlanBuilder(records, config)
        operations = 0
        while not builder.done:
            plan, used = builder.advance(1)
            operations += used
            self.assertLessEqual(used, 1)
            self.assertLess(operations, 1000)
        actual = builder.plan
        self.assertEqual(
            tuple(expected.iter_pairs()),
            tuple(actual.iter_pairs()),
        )
        self.assertEqual(
            actual.diagnostics.theoretical_all_pairs,
            expected.diagnostics.theoretical_all_pairs,
        )
        self.assertEqual(actual.diagnostics.max_bucket, expected.diagnostics.max_bucket)

    def test_pro_worker_uses_only_immutable_graphs_and_one_inflight_slot(self):
        worker = STACK_TOOLS.pro_worker.ProCorrespondenceWorker(max_workers=1)
        graph = _triangle_graph()
        original_finder = STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence
        captured = {}

        def fake_finder(master, candidate, **kwargs):
            captured["master"] = master
            captured["candidate"] = candidate
            captured["kwargs"] = kwargs
            return "worker-result"

        STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence = fake_finder
        try:
            token = worker.submit(
                graph,
                graph,
                allow_flipping=True,
                match_scale=False,
                tolerance=0.01,
                max_search=1024,
            )
            with self.assertRaisesRegex(RuntimeError, "in-flight"):
                worker.submit(
                    graph,
                    graph,
                    allow_flipping=True,
                    match_scale=False,
                    tolerance=0.01,
                    max_search=1024,
                )
            outcome = None
            for _ in range(1000):
                outcome = worker.poll()
                if outcome is not None:
                    break
                time.sleep(0.001)
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome.token, token)
            self.assertEqual(outcome.result, "worker-result")
            self.assertIsInstance(captured["master"], TOPOLOGY.IslandGraph)
            self.assertIsInstance(captured["candidate"], TOPOLOGY.IslandGraph)
            self.assertEqual(captured["kwargs"]["max_search"], 1024)
            self.assertEqual(captured["kwargs"]["cooperative_yield_every"], 64)
            self.assertEqual(worker.snapshot()["worker_in_flight_peak"], 1)
        finally:
            STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence = original_finder
            worker.shutdown()
        self.assertTrue(worker.snapshot()["worker_shutdown"])

    def test_pro_worker_reports_pure_compute_time_and_finder_error(self):
        worker = STACK_TOOLS.pro_worker.ProCorrespondenceWorker(
            max_workers=1,
            cooperative_yield_every=32,
        )
        graph = _triangle_graph()
        original_finder = STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence

        def failing_finder(*_args, **_kwargs):
            raise RuntimeError("synthetic worker failure")

        STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence = failing_finder
        try:
            worker.submit(
                graph,
                graph,
                allow_flipping=True,
                match_scale=True,
                tolerance=0.01,
                max_search=1024,
            )
            outcome = None
            for _ in range(1000):
                outcome = worker.poll()
                if outcome is not None:
                    break
                time.sleep(0.001)
            self.assertIsNotNone(outcome)
            self.assertIn("synthetic worker failure", outcome.error)
            self.assertGreaterEqual(outcome.compute_ms, 0.0)
            snapshot = worker.snapshot()
            self.assertEqual(snapshot["worker_errors"], 1)
            self.assertEqual(snapshot["cooperative_yield_every"], 32)
            self.assertGreaterEqual(snapshot["worker_compute_ms"], 0.0)
        finally:
            STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence = original_finder
            worker.shutdown()

    def test_pro_worker_cancel_discards_result_and_does_not_wait(self):
        worker = STACK_TOOLS.pro_worker.ProCorrespondenceWorker(max_workers=1)
        graph = _triangle_graph()
        started = threading.Event()
        release = threading.Event()
        original_finder = STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence

        def slow_finder(*_args, **_kwargs):
            started.set()
            release.wait(2.0)
            return "late-result"

        STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence = slow_finder
        try:
            worker.submit(
                graph,
                graph,
                allow_flipping=True,
                match_scale=True,
                tolerance=0.01,
                max_search=1024,
            )
            self.assertTrue(started.wait(1.0))
            self.assertTrue(worker.discard())
            self.assertIsNone(worker.poll())
            worker.shutdown()
            self.assertEqual(worker.snapshot()["worker_discards"], 1)
        finally:
            release.set()
            STACK_TOOLS.pro_worker.topology_correspondence.find_correspondence = original_finder
            worker.shutdown()

    def test_pro_session_does_not_stage_before_worker_and_context_invalidates_result(self):
        session = self._session(correspondence_max_search=1024)
        master_key, member_key = (1,), (2,)
        layer = object()
        master_loops = {(0, 0): FakeLoop((1.0, 2.0))}
        candidate_loops = {(7, 0): FakeLoop((9.0, 9.0))}
        session._density_by_key.update({master_key: 2.0, member_key: 1.0})
        session._cheap_signatures.update({master_key: object(), member_key: object()})
        session._key_to_island.update({master_key: object(), member_key: object()})
        session._settings = types.SimpleNamespace(
            stack_allow_flipping=True,
            stack_match_scale=True,
            stack_similarity_tolerance=0.01,
        )
        session.report["shape_fit_accepted"] = 0
        session.uv_layer = layer
        session._graph_for = lambda key: (
            _triangle_graph(0 if key == master_key else 7),
            master_loops if key == master_key else candidate_loops,
            None,
        )
        original_boundary = STACK_TOOLS.similarity_matcher.cheap_boundary_gate
        original_topology = STACK_TOOLS.similarity_matcher.cheap_topology_gate
        original_shape = STACK_TOOLS._pro_shape_match_result
        STACK_TOOLS.similarity_matcher.cheap_boundary_gate = (
            lambda *_args: types.SimpleNamespace(passed=True)
        )
        STACK_TOOLS.similarity_matcher.cheap_topology_gate = (
            lambda *_args: types.SimpleNamespace(passed=True)
        )
        STACK_TOOLS._pro_shape_match_result = lambda *args: types.SimpleNamespace(
            accepted=True,
            score=0.0,
            transform=object(),
        )

        class FakeWorker:
            def __init__(self):
                self.outcome = None
                self.submitted = None

            def submit(self, master, candidate, **kwargs):
                self.submitted = (master, candidate, kwargs)
                return 1

            def poll(self):
                outcome, self.outcome = self.outcome, None
                return outcome

            def in_flight_wall_ms(self):
                return 0.0

            def discard(self):
                return True

            def shutdown(self):
                return None

            def snapshot(self):
                return {"worker_shutdown": False}

        fake_worker = FakeWorker()
        session._worker = fake_worker
        try:
            pair = types.SimpleNamespace(master_key=master_key, member_key=member_key)
            self.assertTrue(session._process_pair(pair))
            self.assertEqual(session._staged_writes, [])
            fake_worker.outcome = STACK_TOOLS.pro_worker.WorkerOutcome(
                token=1,
                result=_correspondence([((7, 0), (0, 0))]),
                wall_ms=0.25,
            )
            outcome = session._poll_worker()
            self.assertIsNotNone(outcome)
            session._consume_worker_outcome(outcome)
            self.assertEqual(len(session._staged_writes), 1)

            session2 = self._session(correspondence_max_search=1024)
            session2._worker = fake_worker
            session2._inflight = {
                "token": 2,
                "master_key": master_key,
                "member_key": member_key,
                "master_loops": master_loops,
                "candidate_loops": candidate_loops,
            }
            invalid_result = STACK_TOOLS.pro_worker.WorkerOutcome(
                token=2,
                result=_correspondence([((7, 0), (0, 0))]),
                wall_ms=0.1,
            )
            original_snapshots = STACK_TOOLS._pro_session_context_valid
            STACK_TOOLS._pro_session_context_valid = lambda *_args: False
            try:
                session2._consume_worker_outcome(invalid_result)
            finally:
                STACK_TOOLS._pro_session_context_valid = original_snapshots
            self.assertTrue(session2.done)
            self.assertEqual(session2.report["exact_loop_writes"], 0)
            self.assertEqual(session2._staged_writes, [])
        finally:
            STACK_TOOLS.similarity_matcher.cheap_boundary_gate = original_boundary
            STACK_TOOLS.similarity_matcher.cheap_topology_gate = original_topology
            STACK_TOOLS._pro_shape_match_result = original_shape
            if not session.done:
                session.cancel("test_cleanup")

    def test_pro_modal_tick_forwards_one_correspondence_limit_and_cleans_on_finish(self):
        operator = STACK_TOOLS.UVGPT_OT_align_similar_pro_fast()

        class FakeSession:
            done = False
            cancelled = False
            error = None
            report = {"selected_count": 2, "aligned_exact": 0}

            def step(self, **kwargs):
                self.kwargs = kwargs
                self.done = True
                return {"done": True}

            def cancel(self, reason):
                self.cancelled = True
                self.report["cancel_reason"] = reason
                self.done = True

        session = FakeSession()
        operator._session = session
        operator.report = lambda *_args: None
        original_end = STACK_TOOLS._pro_modal_progress_end
        STACK_TOOLS._pro_modal_progress_end = lambda _context: None
        try:
            result = operator.modal(None, types.SimpleNamespace(type="TIMER"))
        finally:
            STACK_TOOLS._pro_modal_progress_end = original_end

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(session.kwargs["max_correspondence"], 1)
        self.assertEqual(session.kwargs["active_budget_ms"], 12.0)


if __name__ == "__main__":
    unittest.main()
