"""Pure route-closure tests for the Pro stack/session boundary."""

from types import SimpleNamespace
import unittest
from unittest import mock

from test_align_similar_selected import STACK_TOOLS


PAYLOAD = STACK_TOOLS.pro_process_payload
FAST = PAYLOAD.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
EXACT = PAYLOAD.CORRESPONDENCE_MODE_EXACT_ONLY
HYBRID = PAYLOAD.CORRESPONDENCE_MODE_HYBRID


class ProRouteClosureTests(unittest.TestCase):
    def setUp(self):
        self._missing = object()
        self._old_settings = getattr(
            STACK_TOOLS.uv_utils,
            "get_settings",
            self._missing,
        )
        STACK_TOOLS.uv_utils.get_settings = lambda _context: SimpleNamespace(
            stack_similarity_tolerance=0.01,
            stack_allow_flipping=True,
            stack_match_scale=True,
        )

    def tearDown(self):
        if self._old_settings is self._missing:
            del STACK_TOOLS.uv_utils.get_settings
        else:
            STACK_TOOLS.uv_utils.get_settings = self._old_settings

    def _session(self, mode=HYBRID, **kwargs):
        return STACK_TOOLS._ProAlignSession(
            None,
            None,
            None,
            None,
            selected_islands=[],
            all_islands=[],
            evidence={},
            correspondence_mode=mode,
            **kwargs,
        )

    def _fused_session(self, mode):
        session = self._session(
            mode,
            process_worker_count=1,
            process_batch_size=16,
            process_test_override=True,
            process_fused=True,
            process_group_first=False,
        )
        identity = PAYLOAD.SnapshotIdentity("route-closure", 0, "snapshot")
        session._process_identity = identity
        session._process_graph_context = SimpleNamespace(
            context_digest="context",
            fused_digest="fused",
        )
        session._process_options = PAYLOAD.ExactOptions()
        session._process_fused_descriptors = {
            (0,): SimpleNamespace(descriptor_digest="master-digest"),
            (1,): SimpleNamespace(descriptor_digest="member-digest"),
        }
        session._process_island_loop_keys = {
            (0,): ((0, 0), (0, 1)),
            (1,): ((1, 0), (1, 1)),
        }
        session._process_pair_contexts = {
            0: {
                "master_key": (0,),
                "member_key": (1,),
                "shape_prefilter": None,
            }
        }
        return session

    def test_non_group_first_fused_specs_are_explicit_for_all_modes(self):
        for mode in (FAST, EXACT, HYBRID):
            session = self._fused_session(mode)
            task = session._build_process_fused_batch_for_ordinals((0,))
            pair = task.pair_tasks[0]
            self.assertEqual(task.correspondence_mode, mode)
            self.assertEqual(pair.correspondence_mode, mode)
            self.assertEqual(pair.to_wire()[0], "fused-pair-v2")
            self.assertEqual(pair.to_wire()[-1], mode)

    def test_synchronous_fast_rejects_before_any_solver_or_apply(self):
        expected = (
            "Pro Fast requires an external correspondence worker; synchronous "
            "mode cannot provide verified-nearest-only semantics."
        )
        with mock.patch.object(
            STACK_TOOLS.topology_correspondence,
            "CorrespondenceSearch",
            side_effect=AssertionError("synchronous Fast called exact"),
        ) as exact, mock.patch.object(
            STACK_TOOLS.pro_verified_nearest,
            "find_verified_nearest",
            side_effect=AssertionError("synchronous Fast called nearest"),
        ) as nearest, mock.patch.object(
            STACK_TOOLS,
            "_pro_apply_staged_writes",
            side_effect=AssertionError("synchronous Fast applied UVs"),
        ) as apply:
            with mock.patch.object(
                STACK_TOOLS.uv_utils,
                "ensure_destructive_ready",
                side_effect=AssertionError("Fast crossed request boundary"),
                create=True,
            ) as ready:
                with self.assertRaisesRegex(RuntimeError, expected):
                    STACK_TOOLS._pro_create_session(
                        None,
                        mode=FAST,
                        process_worker_count=None,
                    )
            ready.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, expected):
                self._session(FAST)
        exact.assert_not_called()
        nearest.assert_not_called()
        apply.assert_not_called()

    def test_synchronous_exact_constructs_only_correspondence_search(self):
        session = self._session(EXACT)
        master_key, member_key = (0,), (1,)
        session._uv_area_by_key.update({master_key: 4.0, member_key: 1.0})
        session._density_by_key.update({master_key: 1.0, member_key: 100.0})
        session._cheap_signatures.update(
            {master_key: object(), member_key: object()}
        )
        session._settings = SimpleNamespace(
            stack_allow_flipping=True,
            stack_match_scale=True,
            stack_similarity_tolerance=0.01,
        )
        session.report["shape_fit_accepted"] = 0
        session._advance_shape_for = mock.Mock(
            return_value=("ready", SimpleNamespace(accepted=True), None)
        )
        session._advance_graph_for = mock.Mock(
            side_effect=(
                ("ready", object(), {(0, 0): object()}, None),
                ("ready", object(), {(1, 0): object()}, None),
            )
        )
        with mock.patch.object(
            STACK_TOOLS.similarity_matcher,
            "cheap_boundary_gate",
            return_value=SimpleNamespace(passed=True),
        ), mock.patch.object(
            STACK_TOOLS.similarity_matcher,
            "cheap_topology_gate",
            return_value=SimpleNamespace(passed=True),
        ), mock.patch.object(
            STACK_TOOLS,
            "_selected_match_passes_quality",
            return_value=True,
        ), mock.patch.object(
            STACK_TOOLS.topology_correspondence,
            "CorrespondenceSearch",
            return_value=object(),
        ) as exact, mock.patch.object(
            STACK_TOOLS.pro_verified_nearest,
            "find_verified_nearest",
            side_effect=AssertionError("synchronous Exact called nearest"),
        ) as nearest:
            accepted = session._process_pair(
                SimpleNamespace(master_key=master_key, member_key=member_key)
            )
        self.assertTrue(accepted)
        exact.assert_called_once()
        nearest.assert_not_called()

    def test_group_first_report_wins_over_fused_report_label(self):
        session = self._session(
            HYBRID,
            process_worker_count=1,
            process_batch_size=16,
            process_test_override=True,
            process_fused=True,
            process_group_first=True,
        )
        session._process_pool = SimpleNamespace(worker_pids=(1234,))
        session._process_last_progress = None
        session._update_worker_report()
        self.assertEqual(
            session.report["worker_mode"],
            "external_bundled_python_group_first",
        )

    def test_uv_area_precedence_beats_density_and_has_stable_tie(self):
        areas = {(0,): 1.0, (1,): 4.0}
        self.assertTrue(
            STACK_TOOLS._pro_master_precedes(
                (1,), (0,), None, uv_area_by_key=areas
            )
        )
        self.assertFalse(
            STACK_TOOLS._pro_master_precedes(
                (0,), (1,), None, uv_area_by_key=areas
            )
        )
        tie = {(9,): 3.0, (2,): 3.0}
        self.assertTrue(
            STACK_TOOLS._pro_master_precedes(
                (2,), (9,), None, uv_area_by_key=tie
            )
        )
        self.assertFalse(
            STACK_TOOLS._pro_master_precedes(
                (9,), (2,), None, uv_area_by_key=tie
            )
        )

    def test_missing_uv_area_rejects_without_density_fallback(self):
        session = self._session(EXACT)
        session._uv_area_by_key[(0,)] = None
        session._uv_area_by_key[(1,)] = 100.0
        session._density_by_key[(0,)] = 999.0
        session._density_by_key[(1,)] = 1.0
        self.assertEqual(
            STACK_TOOLS._pro_master_precedence_reason(
                (0,), (1,), session._uv_area_by_key
            ),
            "missing_uv_area",
        )
        prefilter = session._process_shape_prefilter(
            (0,),
            (1,),
            allow_master_precedence=True,
        )
        self.assertIsNotNone(prefilter)
        self.assertEqual(prefilter.reason, "missing_uv_area")

        prepared = self._session(EXACT)
        prepared._prepare_completed = True
        prepared._uv_area_by_key.update({(0,): None, (1,): 100.0})
        prepared._density_by_key.update({(0,): 999.0, (1,): 1.0})
        self.assertFalse(
            prepared._process_pair(
                SimpleNamespace(master_key=(0,), member_key=(1,))
            )
        )

        area_master = self._session(EXACT)
        area_master._uv_area_by_key.update({(0,): 4.0, (1,): 1.0})
        area_master._density_by_key.update({(0,): 1.0, (1,): 100.0})
        area_master._cheap_signatures.update({(0,): object(), (1,): object()})
        with mock.patch.object(
            STACK_TOOLS.similarity_matcher,
            "cheap_boundary_gate",
            return_value=SimpleNamespace(passed=True),
        ), mock.patch.object(
            STACK_TOOLS.similarity_matcher,
            "cheap_topology_gate",
            return_value=SimpleNamespace(passed=True),
        ):
            self.assertIsNone(
                area_master._process_shape_prefilter(
                    (0,),
                    (1,),
                    allow_master_precedence=True,
                )
            )


if __name__ == "__main__":
    unittest.main()
