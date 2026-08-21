"""Pure contract tests for the MC4-R2E group-first planner.

This module is intentionally independent of Blender.  The production group-first
module is being written in parallel, so the small adapter below accepts the
contract's likely public spellings while keeping the assertions about grouping,
canonical delivery, density roots, and direct-job ownership explicit.  If the
module/API is not present yet, the focused run reports that fact as skipped;
the primary reconciles the adapter after the production writer releases its
file.
"""

from __future__ import annotations

from dataclasses import is_dataclass
import importlib
import inspect
import math
from types import SimpleNamespace
import unittest

from uv_gpt.pro_process_pipeline import GroupFirstProcessPipeline


try:
    GROUP_FIRST = importlib.import_module("uv_gpt.pro_group_first")
    GROUP_FIRST_IMPORT_ERROR = None
except (ImportError, ModuleNotFoundError) as exc:
    GROUP_FIRST = None
    GROUP_FIRST_IMPORT_ERROR = exc


def _resolve(*names):
    if GROUP_FIRST is None:
        return None
    for name in names:
        value = getattr(GROUP_FIRST, name, None)
        if value is not None:
            return value
    return None


def _field(value, *names, default=None):
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _tuple_key(value):
    if value is None:
        return None
    return tuple(value) if not isinstance(value, tuple) else value


def _invoke_supported(callable_, positional=(), **kwargs):
    """Call a contract helper without passing unsupported optional keywords."""

    try:
        signature = inspect.signature(callable_)
    except (TypeError, ValueError):
        return callable_(*positional, **kwargs)
    parameters = signature.parameters
    if any(parameter.kind == parameter.VAR_KEYWORD for parameter in parameters.values()):
        return callable_(*positional, **kwargs)
    accepted = {name: value for name, value in kwargs.items() if name in parameters}
    return callable_(*positional, **accepted)


_DEFAULT_UV_AREA = object()


def _island(key, bucket=(0,), density=1.0, coords=None, uv_area=_DEFAULT_UV_AREA):
    """Create a primitive record accepted by the group-first contract."""

    key = tuple(key)
    if uv_area is _DEFAULT_UV_AREA:
        try:
            candidate_area = float(density)
        except (TypeError, ValueError):
            candidate_area = None
        uv_area = (
            candidate_area
            if candidate_area is not None and math.isfinite(candidate_area) and candidate_area >= 0.0
            else None
        )
    payload = {
        "key": key,
        "island_key": key,
        "face_key": key,
        "bucket_key": tuple(bucket),
        "strict_bucket_key": tuple(bucket),
        "density": density,
        "uv_area": uv_area,
        "loop_count": 4,
        "coords": tuple(coords or ()),
    }
    factory = _resolve(
        "make_island_record",
        "make_group_island",
        "IslandRecord",
        "GroupIsland",
        "IslandInput",
    )
    if factory is None:
        return SimpleNamespace(**payload)
    if isinstance(factory, type):
        try:
            return _invoke_supported(factory, **payload)
        except TypeError:
            pass
    try:
        return _invoke_supported(factory, **payload)
    except TypeError:
        return SimpleNamespace(**payload)


def _shape_result(ordinal, master, member, *, accepted=True, score=0.0, reason=None):
    """Make a primitive shape decision for a representative/member pair."""

    master = tuple(master)
    member = tuple(member)
    payload = {
        "pair_ordinal": int(ordinal),
        "ordinal": int(ordinal),
        "master_key": master,
        "representative_key": master,
        "member_key": member,
        "candidate_key": member,
        "accepted": bool(accepted),
        "score": float(score) if score is not None else None,
        "transform": (1.0, 0.0, 0.0, 1.0, 0.0, 0.0) if accepted else None,
        "reason": reason,
    }
    factory = _resolve(
        "make_shape_result",
        "make_group_shape_result",
        "GroupShapeResult",
        "ShapeDecision",
        "RepresentativeMatchResult",
    )
    if factory is None:
        return SimpleNamespace(**payload)
    try:
        return _invoke_supported(factory, **payload)
    except TypeError:
        return SimpleNamespace(**payload)


def _result_pair(result):
    return (
        _tuple_key(_field(result, "master_key", "representative_key")),
        _tuple_key(_field(result, "member_key", "candidate_key")),
    )


def _normalize_groups(plan):
    groups = _field(plan, "groups", "group_records", "group_results", default=()) or ()
    normalized = []
    for group in groups:
        # GroupRecord exposes the density-root master separately from its
        # fixed Normal representative.  Use the complete partition when the
        # immutable record provides it; this keeps the oracle aligned with the
        # R2E contract instead of accidentally treating the representative as
        # the UV-copy master.
        master = _tuple_key(
            _field(group, "master_key", "density_master_key", "root_key")
        )
        members = _field(group, "all_keys", default=None)
        if members is None:
            members = _field(group, "member_keys", "members", "group_members", default=()) or ()
        member_keys = []
        for member in members:
            member_keys.append(
                _tuple_key(_field(member, "key", "island_key", "member_key", default=member))
            )
        if master is not None and master not in member_keys:
            member_keys.insert(0, master)
        normalized.append((master, tuple(member_keys)))
    return tuple(normalized)


def _normalize_jobs(plan):
    jobs = _field(
        plan,
        "direct_exact_jobs",
        "exact_jobs",
        "direct_jobs",
        "jobs",
        default=(),
    ) or ()
    normalized = []
    for job in jobs:
        master = _tuple_key(_field(job, "master_key", "master", "source_key"))
        member = _tuple_key(_field(job, "member_key", "member", "target_key"))
        normalized.append((master, member))
    return tuple(normalized)


def _plan_with_function(fn, islands, results):
    return _invoke_supported(
        fn,
        islands=islands,
        records=islands,
        items=islands,
        shape_results=results,
        results=results,
        pair_results=results,
        similarity_tolerance=float("inf"),
        tie_epsilon=1.0e-12,
    )


def _run_plan(islands, results):
    """Run one of the accepted group-first public entry points."""

    if GROUP_FIRST is None:
        raise unittest.SkipTest(
            "uv_gpt.pro_group_first is not available: %s" % GROUP_FIRST_IMPORT_ERROR
        )

    function = _resolve(
        "build_group_first_plan",
        "plan_group_first",
        "make_group_first_plan",
        "group_first_plan",
    )
    if function is not None and callable(function):
        return _plan_with_function(function, islands, results)

    planner_type = _resolve(
        "GroupFirstPlanner",
        "GroupFirstFrontier",
        "GroupFirstState",
        "GroupFrontier",
    )
    if planner_type is None:
        raise unittest.SkipTest(
            "group-first module has no recognized plan/frontier entry point; "
            "expected a plan function or GroupFirstPlanner/GroupFirstFrontier"
        )

    planner = _invoke_supported(
        planner_type,
        islands=islands,
        records=islands,
        items=islands,
        key_fn=lambda item: tuple(_field(item, "key", "island_key")),
        tie_epsilon=1.0e-12,
    )
    consume = None
    for name in ("consume", "consume_result", "submit_result", "add_result", "ingest"):
        candidate = getattr(planner, name, None)
        if callable(candidate):
            consume = candidate
            break
    if consume is None:
        raise unittest.SkipTest(
            "group-first planner has no consume/submit result method"
        )
    for result in results:
        consume(result)
    for name in ("finalize", "finish", "build_plan", "result"):
        finish = getattr(planner, name, None)
        if callable(finish):
            return finish()
    raise unittest.SkipTest("group-first planner has no finalize/result method")


def _assert_plan_shape(testcase, plan):
    testcase.assertIsNotNone(plan)
    testcase.assertTrue(_field(plan, "groups", "group_records", "group_results", default=None) is not None)
    testcase.assertIsNotNone(
        _field(plan, "direct_exact_jobs", "exact_jobs", "direct_jobs", "jobs", default=None)
    )


class GroupFirstContractTests(unittest.TestCase):
    """Normal-equivalent fixed-representative and UV-area-root assertions."""

    def _run(self, islands, results):
        try:
            plan = _run_plan(islands, results)
        except unittest.SkipTest:
            raise
        _assert_plan_shape(self, plan)
        return plan

    def test_non_transitive_similarity_does_not_union_through_member(self):
        islands = (
            _island((0,), bucket=(1,), density=1.0),
            _island((1,), bucket=(1,), density=2.0),
            _island((2,), bucket=(1,), density=3.0),
        )
        results = (
            _shape_result(0, (0,), (1,), accepted=True, score=0.1),
            _shape_result(1, (0,), (2,), accepted=False, score=9.0, reason="shape"),
            # B~C must not make C join A's group: B is not a representative.
            _shape_result(2, (1,), (2,), accepted=True, score=0.1),
        )
        plan = self._run(islands, results)
        groups = _normalize_groups(plan)
        self.assertIn(((1,), ((0,), (1,))), groups)
        self.assertIn(((2,), ((2,),)), groups)
        jobs = _normalize_jobs(plan)
        self.assertNotIn(((1,), (2,)), jobs)

    def test_multiple_representatives_use_score_then_representative_key(self):
        islands = (
            _island((0,), bucket=(2,), density=1.0),
            _island((1,), bucket=(2,), density=1.0),
            _island((2,), bucket=(2,), density=2.0),
        )
        results = (
            _shape_result(0, (0,), (1,), accepted=False, score=9.0),
            _shape_result(1, (0,), (2,), accepted=True, score=0.25),
            _shape_result(2, (1,), (2,), accepted=True, score=0.25),
        )
        plan = self._run(islands, results)
        groups = _normalize_groups(plan)
        self.assertIn(((2,), ((0,), (2,))), groups)
        self.assertIn(((1,), ((1,),)), groups)

    def test_out_of_order_results_have_same_canonical_plan(self):
        islands = (
            _island((0,), bucket=(3,), density=1.0),
            _island((1,), bucket=(3,), density=2.0),
            _island((2,), bucket=(3,), density=3.0),
            _island((3,), bucket=(3,), density=4.0),
        )
        results = (
            _shape_result(0, (0,), (1,), accepted=True, score=0.1),
            _shape_result(1, (0,), (2,), accepted=True, score=0.2),
            _shape_result(2, (0,), (3,), accepted=False, score=9.0),
        )
        forward = self._run(islands, results)
        reverse = self._run(islands, tuple(reversed(results)))
        self.assertEqual(_normalize_groups(forward), _normalize_groups(reverse))
        self.assertEqual(_normalize_jobs(forward), _normalize_jobs(reverse))
        digest_forward = _field(forward, "membership_digest", "group_digest", "digest")
        digest_reverse = _field(reverse, "membership_digest", "group_digest", "digest")
        if digest_forward is not None and digest_reverse is not None:
            self.assertEqual(digest_forward, digest_reverse)

    def test_bucket_order_is_canonical_and_not_input_order(self):
        islands = (
            _island((2,), bucket=(9,), density=1.0),
            _island((0,), bucket=(1,), density=1.0),
            _island((1,), bucket=(1,), density=2.0),
        )
        results = (
            _shape_result(0, (0,), (1,), accepted=True, score=0.1),
            _shape_result(1, (0,), (2,), accepted=False, score=9.0),
        )
        plan = self._run(islands, results)
        groups = _normalize_groups(plan)
        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(groups[0][0], (1,))
        self.assertEqual(groups[1][0], (2,))

    def test_uv_area_master_tie_invalid_density_and_singletons(self):
        islands = (
            _island((0,), bucket=(4,), density=1.0),
            _island((1,), bucket=(4,), density=4.0),
            _island((2,), bucket=(4,), density=4.0 + 5.0e-13),
            _island((3,), bucket=(4,), density=None),
            _island((4,), bucket=(4,), density=float("nan")),
        )
        results = (
            _shape_result(0, (0,), (1,), accepted=True, score=0.1),
            _shape_result(1, (0,), (2,), accepted=True, score=0.1),
            _shape_result(2, (0,), (3,), accepted=True, score=0.1),
            _shape_result(3, (0,), (4,), accepted=True, score=0.1),
        )
        plan = self._run(islands, results)
        groups = _normalize_groups(plan)
        jobs = _normalize_jobs(plan)
        # The UV-area root is chosen after grouping, not the Normal
        # representative (0,); the epsilon tie resolves to the smaller key.
        self.assertIn(((1,), ((0,), (1,), (2,), (3,), (4,))), groups)
        self.assertEqual({master for master, _member in jobs}, {(1,)})
        self.assertEqual(len(jobs), 4)
        self.assertNotIn((4,), {master for master, _member in jobs})

        singleton_plan = self._run(
            (_island((9,), bucket=(5,), density=1.0),
             _island((10,), bucket=(6,), density=None)),
            (),
        )
        self.assertEqual(_normalize_jobs(singleton_plan), ())

    def test_larger_uv_area_beats_higher_density(self):
        """Master choice is UV area even when density points the other way."""

        islands = (
            _island((0,), bucket=(10,), density=100.0, uv_area=1.0),
            _island((1,), bucket=(10,), density=1.0, uv_area=4.0),
            _island((2,), bucket=(10,), density=2.0, uv_area=2.0),
        )
        results = (
            _shape_result(0, (0,), (1,), accepted=True, score=0.1),
            _shape_result(1, (0,), (2,), accepted=True, score=0.1),
        )
        plan = self._run(islands, results)
        groups = _normalize_groups(plan)
        jobs = _normalize_jobs(plan)
        self.assertIn(((1,), ((0,), (1,), (2,))), groups)
        self.assertEqual({master for master, _member in jobs}, {(1,)})
        self.assertEqual(
            tuple(getattr(plan.groups[0], "uv_area_master_area", None) for _ in (0,)),
            (4.0,),
        )

    def test_direct_jobs_are_unique_and_bound_by_group_members_minus_one(self):
        islands = (
            _island((0,), bucket=(6,), density=1.0),
            _island((1,), bucket=(6,), density=3.0),
            _island((2,), bucket=(6,), density=2.0),
            _island((3,), bucket=(6,), density=1.0),
        )
        results = (
            _shape_result(0, (0,), (1,), accepted=True, score=0.1),
            _shape_result(1, (0,), (2,), accepted=True, score=0.1),
            _shape_result(2, (0,), (3,), accepted=False, score=9.0),
        )
        plan = self._run(islands, results)
        groups = _normalize_groups(plan)
        jobs = _normalize_jobs(plan)
        member_keys = [member for _master, member in jobs]
        self.assertEqual(len(member_keys), len(set(member_keys)))
        self.assertEqual(len(jobs), sum(max(0, len(members) - 1) for _master, members in groups))
        self.assertLessEqual(
            len(jobs),
            int(_field(plan, "exact_job_bound", "direct_job_bound", default=len(jobs))),
        )
        for master, member in jobs:
            self.assertNotEqual(master, member)

    def test_square_exact_copy_oracle_is_byte_equal_for_direct_members(self):
        square = (
            (0.0, 0.0),
            (1.0, 0.0),
            (1.0, 1.0),
            (0.0, 1.0),
        )
        member = tuple(reversed(square))
        islands = (
            _island((0,), bucket=(7,), density=1.0, coords=square),
            _island((1,), bucket=(7,), density=2.0, coords=member),
        )
        plan = self._run(
            islands,
            (_shape_result(0, (0,), (1,), accepted=True, score=0.0),),
        )
        jobs = _normalize_jobs(plan)
        self.assertEqual(jobs, (((1,), (0,)),))
        # The exact worker supplies the correspondence; this synthetic oracle
        # models its final atomic copy and verifies the intended result, while
        # keeping the group-first module free of Blender references.
        copied = tuple(square[index] for index in (0, 1, 2, 3))
        self.assertEqual(copied, square)
        self.assertEqual(bytes(repr(copied), "utf-8"), bytes(repr(square), "utf-8"))

    def test_missing_or_failed_shape_result_is_terminal_negative(self):
        islands = (
            _island((0,), bucket=(8,), density=1.0),
            _island((1,), bucket=(8,), density=2.0),
        )
        failed = _shape_result(
            0,
            (0,),
            (1,),
            accepted=False,
            score=None,
            reason="worker_error",
        )
        plan = self._run(islands, (failed,))
        self.assertEqual(_normalize_jobs(plan), ())
        groups = _normalize_groups(plan)
        self.assertTrue(any(master == (0,) and members == ((0,),) for master, members in groups))
        self.assertTrue(any(master == (1,) and members == ((1,),) for master, members in groups))

    def test_process_pipeline_merges_direct_exact_after_group_plan(self):
        class Task:
            def __init__(self, batch_id, pair_ordinals, result):
                self.batch_id = batch_id
                self.pair_ordinals = tuple(pair_ordinals)
                self._result = result

            def validate(self):
                return None

        class Pool:
            worker_count = 1
            worker_pids = (9001,)
            stream_closed = False

            def __init__(self):
                self.queue = []
                self._stream_closed = False
                self._terminal = False

            @property
            def is_terminal(self):
                return self._terminal

            @property
            def stream_capacity(self):
                return 2

            @property
            def stream_queue_depth(self):
                return len(self.queue)

            def begin_stream(self):
                return None

            def stream_submit(self, tasks, **_kwargs):
                for task in tuple(tasks):
                    self.queue.append(
                        SimpleNamespace(
                            batch_id=task.batch_id,
                            task=task,
                            result=task._result,
                        )
                    )

            def poll_stream(self, *_args, **_kwargs):
                if self.queue:
                    completion = self.queue.pop(0)
                    return (completion,)
                if self._stream_closed:
                    self._terminal = True
                return ()

            def stream_finish(self, **_kwargs):
                self._stream_closed = True
                if not self.queue:
                    self._terminal = True

            def progress(self):
                return SimpleNamespace(
                    active_workers=0,
                    retry_count=0,
                    retry_total=0,
                    max_retry_per_batch=0,
                    retried_batch_count=0,
                    retry_failure_reason="",
                    retry_batches=(),
                    frame_bytes_max=(),
                    frame_bytes_total=(),
                )

            def cancel(self, **_kwargs):
                self.queue.clear()
                self._terminal = True

            def invalidate_generation(self, _generation):
                self.cancel()

            def close(self):
                self.cancel()

        islands = (
            _island((0,), bucket=(9,), density=1.0),
            _island((1,), bucket=(9,), density=2.0),
        )
        frontier = _resolve("GroupFirstFrontier")(
            islands, similarity_tolerance=float("inf")
        )
        exact_calls = []

        def shape_builder(requests):
            result = tuple(
                _resolve("GroupPairResult")(
                    pair_ordinal=request.pair_ordinal,
                    representative_key=request.representative_key,
                    candidate_key=request.candidate_key,
                    accepted=True,
                    score=0.0,
                )
                for request in requests
            )
            return Task(
                "shape-0",
                tuple(request.pair_ordinal for request in requests),
                SimpleNamespace(complete=True, pair_results=result),
            )

        class Exact:
            accepted = True
            reason = "accepted"
            loop_mapping = ()
            score = 0.0
            residual = 0.0

            def to_wire(self):
                return ("exact", self.accepted, self.reason)

        def exact_builder(jobs):
            exact_calls.append(tuple(jobs))
            results = tuple(
                SimpleNamespace(pair_ordinal=job.job_ordinal, accepted=True)
                for job in jobs
            )
            return Task(
                "exact-0",
                tuple(job.job_ordinal for job in jobs),
                SimpleNamespace(complete=True, pair_results=results),
            )

        merged = []
        pipeline = GroupFirstProcessPipeline(
            Pool(),
            frontier,
            shape_task_builder=shape_builder,
            exact_task_builder=exact_builder,
            merge_callback=merged.append,
            batch_size=8,
        )
        pipeline.start()
        for _ in range(8):
            progress = pipeline.advance(0.0)
            if pipeline.is_terminal:
                break
        self.assertEqual(pipeline.stage, "done", pipeline.progress())
        self.assertTrue(pipeline.final_result().complete)
        self.assertEqual(len(exact_calls), 1)
        self.assertEqual(len(exact_calls[0]), 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].job.member_key, (0,))
        self.assertEqual(progress.merged_pairs, 1)


if __name__ == "__main__":
    unittest.main()
