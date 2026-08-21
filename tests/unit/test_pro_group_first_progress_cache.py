"""Focused pure tests for the immutable group-progress view cache."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import pickle
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from uv_gpt import pro_group_first as group_first
from uv_gpt.pro_process_pipeline import GroupFirstProcessPipeline


_IDENTITY_TRANSFORM = (
    0.0,
    1.0,
    False,
    (0.0, 0.0),
    (0.0, 0.0),
)


def _island(key, *, bucket=(0,), density=1.0, uv_area=None):
    if uv_area is None:
        uv_area = density
    return group_first.IslandRecord(
        key=(key,),
        bucket_key=bucket,
        density=density,
        uv_area=uv_area,
    )


def _result(request, *, accepted=True, score=0.0):
    return group_first.GroupPairResult(
        pair_ordinal=request.pair_ordinal,
        representative_key=request.representative_key,
        candidate_key=request.candidate_key,
        accepted=accepted,
        score=score,
        transform=_IDENTITY_TRANSFORM if accepted else None,
    )


def _consume_all(frontier, *, accepted=True):
    while frontier.pending_requests():
        for request in tuple(frontier.pending_requests()):
            frontier.consume(_result(request, accepted=accepted))
    return frontier.finalize()


class _IdlePool:
    worker_count = 1
    worker_pids = ()
    worker_task_distribution = ()
    startup_timings_ms = ()
    stream_queue_depth = 0
    stream_closed = False
    is_terminal = False

    @property
    def stream_capacity(self):
        return 0

    def begin_stream(self):
        return None

    def progress(self):
        return SimpleNamespace(
            active_workers=0,
            retry_count=0,
            retry_total=0,
            max_retry_per_batch=0,
            frame_bytes_max=(),
            frame_bytes_total=(),
        )


class GroupFirstProgressCacheTests(unittest.TestCase):
    def test_repeated_groups_reuses_immutable_view_and_master_helpers(self):
        frontier = group_first.GroupFirstFrontier(
            (_island(1, density=1.0), _island(2, density=2.0), _island(3, density=3.0)),
            similarity_tolerance=float("inf"),
        )

        with patch.object(
            group_first, "_density_master", wraps=group_first._density_master
        ) as density_master, patch.object(
            group_first, "_uv_area_master", wraps=group_first._uv_area_master
        ) as uv_area_master:
            first = frontier.groups
            second = frontier.groups
            third = frontier.groups

            self.assertIs(first, second)
            self.assertIs(second, third)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 1)
            self.assertEqual(density_master.call_count, 1)
            self.assertEqual(uv_area_master.call_count, 1)
            self.assertIsInstance(first, tuple)
            self.assertTrue(
                getattr(type(first[0]), "__dataclass_params__").frozen
            )

    def test_member_append_invalidates_once_and_recomputes_expected_record(self):
        frontier = group_first.GroupFirstFrontier(
            (_island(1), _island(2), _island(3)),
            similarity_tolerance=float("inf"),
        )

        with patch.object(
            group_first, "_density_master", wraps=group_first._density_master
        ) as density_master, patch.object(
            group_first, "_uv_area_master", wraps=group_first._uv_area_master
        ) as uv_area_master:
            before = frontier.groups
            before_epoch = frontier._groups_membership_epoch
            request = frontier.pending_requests()[0]
            frontier.consume(_result(request))
            after = frontier.groups

            self.assertIsNot(before, after)
            self.assertEqual(frontier._groups_membership_epoch, before_epoch + 1)
            self.assertEqual(after[0].representative_key, (1,))
            self.assertEqual(after[0].member_keys, ((2,),))
            self.assertEqual(density_master.call_count, 2)
            self.assertEqual(uv_area_master.call_count, 2)
            self.assertIs(after, frontier.groups)
            self.assertEqual(density_master.call_count, 2)
            self.assertEqual(uv_area_master.call_count, 2)

    def test_new_group_invalidates_view_without_recomputing_on_poll_only(self):
        frontier = group_first.GroupFirstFrontier(
            (
                _island(1, bucket=("a",)),
                _island(2, bucket=("a",)),
                _island(3, bucket=("b",)),
                _island(4, bucket=("b",)),
            ),
            similarity_tolerance=float("inf"),
        )

        with patch.object(
            group_first, "_density_master", wraps=group_first._density_master
        ) as density_master, patch.object(
            group_first, "_uv_area_master", wraps=group_first._uv_area_master
        ) as uv_area_master:
            before = frontier.groups
            before_epoch = frontier._groups_membership_epoch
            frontier.consume(_result(frontier.pending_requests()[0], accepted=False))
            after = frontier.groups

            self.assertIsNot(before, after)
            self.assertEqual(frontier._groups_membership_epoch, before_epoch + 2)
            self.assertEqual(
                tuple(group.representative_key for group in after),
                ((1,), (2,), (3,)),
            )
            self.assertEqual(density_master.call_count, 4)
            self.assertEqual(uv_area_master.call_count, 4)
            self.assertIs(after, frontier.groups)
            self.assertEqual(density_master.call_count, 4)
            self.assertEqual(uv_area_master.call_count, 4)

    def test_final_plan_matches_uncached_semantics_and_reuses_final_tuple(self):
        islands = (
            _island(1, density=1.0, uv_area=1.0),
            _island(2, density=9.0, uv_area=4.0),
            _island(3, density=3.0, uv_area=2.0),
            _island(4, density=4.0, uv_area=3.0),
        )
        cached_frontier = group_first.GroupFirstFrontier(
            islands, similarity_tolerance=float("inf")
        )
        cached_plan = _consume_all(cached_frontier)
        cached_groups = cached_frontier.groups

        # This deliberately bypasses the cache on an independent frontier to
        # keep an uncached semantic reference for group records and plan data.
        reference_frontier = group_first.GroupFirstFrontier(
            islands, similarity_tolerance=float("inf")
        )
        reference_plan = _consume_all(reference_frontier)
        uncached_groups = tuple(
            reference_frontier._freeze_group(index, group)
            for index, group in enumerate(reference_frontier._groups)
        )

        self.assertEqual(cached_plan.groups, uncached_groups)
        self.assertEqual(cached_plan.groups, reference_plan.groups)
        self.assertEqual(cached_plan.exact_jobs, reference_plan.exact_jobs)
        self.assertEqual(cached_plan.direct_exact_jobs, reference_plan.direct_exact_jobs)
        self.assertEqual(cached_plan.membership_digest, reference_plan.membership_digest)
        self.assertEqual(cached_plan.exact_jobs_digest, reference_plan.exact_jobs_digest)
        self.assertIs(cached_groups, cached_plan.groups)
        self.assertIs(cached_groups, cached_frontier.groups)
        self.assertEqual(cached_plan.groups[0].density_master_key, (2,))
        self.assertEqual(cached_plan.groups[0].uv_area_master_key, (2,))

    def test_pipeline_progress_reuses_view_without_completion(self):
        frontier = group_first.GroupFirstFrontier(
            (_island(1), _island(2)), similarity_tolerance=float("inf")
        )
        pipeline = GroupFirstProcessPipeline(
            _IdlePool(),
            frontier,
            shape_task_builder=lambda requests: requests,
            exact_task_builder=lambda jobs: jobs,
        )

        with patch.object(
            group_first, "_density_master", wraps=group_first._density_master
        ) as density_master, patch.object(
            group_first, "_uv_area_master", wraps=group_first._uv_area_master
        ) as uv_area_master:
            pipeline.start()
            progress = [pipeline.progress() for _ in range(6)]

            self.assertEqual(tuple(item.grouping_groups for item in progress), (1,) * 6)
            self.assertEqual(
                tuple(item.grouping_comparisons_planned for item in progress), (1,) * 6
            )
            self.assertEqual(tuple(item.stage for item in progress), ("group_shape_dispatch",) * 6)
            self.assertEqual(density_master.call_count, 1)
            self.assertEqual(uv_area_master.call_count, 1)

    def test_concurrent_progress_and_immutable_encoding_have_one_semantic_view(self):
        frontier = group_first.GroupFirstFrontier(
            (
                _island(1, density=1.0),
                _island(2, density=2.0),
                _island(3, density=3.0),
            ),
            similarity_tolerance=float("inf"),
        )
        pipeline = GroupFirstProcessPipeline(
            _IdlePool(),
            frontier,
            shape_task_builder=lambda requests: requests,
            exact_task_builder=lambda jobs: jobs,
        )
        pipeline.start()

        def read_view(_index):
            progress = pipeline.progress()
            view = frontier.groups
            wire = pickle.dumps(view, protocol=5)
            return (
                view,
                wire,
                hashlib.sha256(wire).hexdigest().upper(),
                (progress.grouping_groups, progress.grouping_comparisons_planned),
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            rows = list(executor.map(read_view, range(64)))

        first_view, first_wire, first_digest, first_progress = rows[0]
        for view, wire, digest, progress in rows[1:]:
            self.assertEqual(view, first_view)
            self.assertEqual(wire, first_wire)
            self.assertEqual(digest, first_digest)
            self.assertEqual(progress, first_progress)
        self.assertEqual(first_progress, (1, 2))
        self.assertTrue(math.isfinite(float(first_view[0].density_master_density)))
        self.assertIs(first_view, frontier.groups)


if __name__ == "__main__":
    unittest.main()
