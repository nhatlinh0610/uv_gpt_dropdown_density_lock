"""Resumable pure shape matching for Align Similar Pro.

The regular similarity matcher remains the synchronous oracle.  This module
uses the same immutable descriptors and the same private numeric primitives,
but advances one bounded descriptor/gate/fit operation at a time so the Pro
modal session can yield between candidate orientations.  It intentionally has
no Blender imports or references.
"""

from dataclasses import replace
import time

from . import similarity_matcher


SHAPE_CALL_LIMIT_MS = 25.0


class ProShapeMatchState:
    """Pause/resume one immutable descriptor match in deterministic order."""

    def __init__(
        self,
        reference=None,
        candidate=None,
        reference_builder=None,
        candidate_builder=None,
        match_scale=True,
        allow_flipping=False,
        tolerance=0.01,
        allow_tolerant_topology=True,
        use_numpy=None,
    ):
        if reference is None and reference_builder is None:
            raise ValueError("reference descriptor or builder is required")
        if candidate is None and candidate_builder is None:
            raise ValueError("candidate descriptor or builder is required")
        self.reference = reference
        self.candidate = candidate
        self._reference_builder = reference_builder
        self._candidate_builder = candidate_builder
        self.match_scale = bool(match_scale)
        self.allow_flipping = bool(allow_flipping)
        self.tolerance = max(0.0, float(tolerance))
        self.allow_tolerant_topology = bool(allow_tolerant_topology)
        if use_numpy is None:
            self.use_numpy = similarity_matcher.numpy_available()
        else:
            self.use_numpy = bool(use_numpy and similarity_matcher.numpy_available())

        self.done = False
        self.cancelled = False
        self.result = None
        self.phase = (
            "reference_descriptor"
            if self.reference is None
            else ("candidate_descriptor" if self.candidate is None else "coarse_gate")
        )
        self.phase_transitions = []
        self.shape_primitive_operations = 0
        self.shape_slices = 0
        self.last_shape_slice_ms = 0.0
        self.last_shape_slice_operations = 0
        self.max_shape_slice_ms = 0.0
        self.max_shape_call_ms = 0.0
        self.over_25ms_calls = 0
        self.over_25ms_call_samples = []

        self._coarse = None
        self._topology = None
        self._structural_penalty = 0.0
        self._reference_loop = None
        self._candidate_loop = None
        self._reference_points = None
        self._candidate_base = None
        self._normalizer = 1.0
        self._reflected_values = (False,)
        self._reflected_index = 0
        self._reversed_index = 0
        self._start_vertex = 0
        self._oriented = None
        self._best_key = None
        self._best_score = None
        self._best_outer_rms = None
        self._best_hole_rms = None
        self._best_transform = None

    def _set_phase(self, phase):
        phase = str(phase)
        if phase != self.phase:
            self.phase_transitions.append((self.phase, phase))
            self.phase_transitions = self.phase_transitions[-64:]
            self.phase = phase

    def _call(self, phase, function):
        started = time.perf_counter()
        value = function()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.max_shape_call_ms = max(self.max_shape_call_ms, elapsed_ms)
        if elapsed_ms > SHAPE_CALL_LIMIT_MS:
            self.over_25ms_calls += 1
            if len(self.over_25ms_call_samples) < 8:
                self.over_25ms_call_samples.append(
                    {"phase": str(phase), "elapsed_ms": float(elapsed_ms)}
                )
        return value

    def _descriptor(self, builder):
        return self._call("descriptor_build", builder)

    def _finish_rejected(self, reason, *, coarse=None, topology=None, score=float("inf"), topology_penalty=0.0):
        self.result = similarity_matcher._rejected(
            reason,
            self._diagnostics,
            coarse=coarse,
            topology=topology,
            score=score,
            topology_penalty=topology_penalty,
        )
        self._set_phase("done")
        self.done = True

    def _advance_one(self):
        if self.phase == "reference_descriptor":
            self.reference = self._descriptor(self._reference_builder)
            self._set_phase("candidate_descriptor")
            return

        if self.phase == "candidate_descriptor":
            self.candidate = self._descriptor(self._candidate_builder)
            self._set_phase("coarse_gate")
            return

        if self.phase == "coarse_gate":
            self._diagnostics = similarity_matcher.MatcherDiagnostics()
            self._coarse = self._call(
                "coarse_gate",
                lambda: similarity_matcher.coarse_boundary_gate(
                    self.reference,
                    self.candidate,
                ),
            )
            if not self._coarse.passed:
                self._finish_rejected(
                    self._coarse.reason,
                    coarse=self._coarse,
                    topology_penalty=self._coarse.penalty,
                )
            else:
                self._diagnostics.coarse_candidates += 1
                self._set_phase("topology_gate")
            return

        if self.phase == "topology_gate":
            self._topology = self._call(
                "topology_gate",
                lambda: similarity_matcher.topology_gate(
                    self.reference,
                    self.candidate,
                ),
            )
            if not self._topology.passed:
                self._finish_rejected(
                    self._topology.reason,
                    coarse=self._coarse,
                    topology=self._topology,
                    topology_penalty=(
                        self._coarse.penalty + self._topology.penalty
                    ),
                )
                return
            self._diagnostics.topology_candidates += 1
            if not self._topology.strict and not self.allow_tolerant_topology:
                self._finish_rejected(
                    "topology_mismatch",
                    coarse=self._coarse,
                    topology=self._topology,
                    topology_penalty=(
                        self._coarse.penalty + self._topology.penalty
                    ),
                )
                return
            self._diagnostics.full_fits += 1
            self._structural_penalty = self._coarse.penalty + self._topology.penalty
            self._set_phase("fit_prepare")
            return

        if self.phase == "fit_prepare":
            self._reference_loop = self.reference.outer_loops[0]
            self._candidate_loop = self.candidate.outer_loops[0]
            if not self.reference.supported or not self.candidate.supported:
                self._finish_rejected(
                    "outer_fit_failed",
                    coarse=self._coarse,
                    topology=self._topology,
                )
                return
            count = min(
                similarity_matcher.MAX_SAMPLE_COUNT,
                max(
                    self._reference_loop.sample_count,
                    self._candidate_loop.sample_count,
                    similarity_matcher.MIN_SAMPLE_COUNT,
                ),
            )
            self._reference_points = self._call(
                "fit_prepare_resample",
                lambda: self._reference_loop.resampled(count),
            )
            if len(self._reference_points) < 3:
                self._finish_rejected(
                    "outer_fit_failed",
                    coarse=self._coarse,
                    topology=self._topology,
                )
                return
            self._candidate_base = self._call(
                "fit_prepare_normalize",
                lambda: similarity_matcher._canonical_start(
                    self._candidate_loop.points,
                    True,
                ),
            )
            self._normalizer = max(
                self._reference_loop.perimeter,
                similarity_matcher.DEGENERATE_EPSILON,
            )
            self._reflected_values = (False, True) if self.allow_flipping else (False,)
            self._reflected_index = 0
            self._reversed_index = 0
            self._start_vertex = 0
            self._oriented = None
            self._set_phase("fit")
            return

        if self.phase == "fit":
            if self._reflected_index >= len(self._reflected_values):
                self._set_phase("result")
                return

            if self._oriented is None:
                if self._reversed_index >= 2:
                    self._reflected_index += 1
                    self._reversed_index = 0
                    self._start_vertex = 0
                    if self._reflected_index >= len(self._reflected_values):
                        self._set_phase("result")
                        return
                reversed_order = self._reversed_index == 1
                base = self._candidate_base
                self._oriented = (
                    (base[0],) + tuple(reversed(base[1:]))
                    if reversed_order
                    else base
                )
                self._start_vertex = 0
                self._reversed_index += 1
                return

            if self._start_vertex >= len(self._oriented):
                self._oriented = None
                return

            reversed_order = self._reversed_index - 1 == 1
            start_vertex = self._start_vertex
            self._start_vertex += 1
            rotated = self._oriented[start_vertex:] + self._oriented[:start_vertex]

            def fit_candidate():
                samples = similarity_matcher.resample_polyline(
                    rotated,
                    len(self._reference_points),
                    closed=True,
                    canonicalize=False,
                )
                return similarity_matcher._fit_one_orientation(
                    self._reference_points,
                    samples,
                    self.match_scale,
                    self._reflected_values[self._reflected_index],
                    self.use_numpy,
                )

            angle, scale, rms, ref_center, cand_center = self._call(
                "fit_candidate",
                fit_candidate,
            )
            normalized = rms / self._normalizer
            transform = similarity_matcher.SimilarityTransform(
                angle=angle,
                scale=scale,
                reflected=self._reflected_values[self._reflected_index],
                reference_center=ref_center,
                candidate_center=cand_center,
                score=normalized,
                rms=rms,
                cyclic_shift=start_vertex,
                reversed=reversed_order,
            )
            score, outer_rms, hole_rms = self._call(
                "score_candidate",
                lambda: similarity_matcher._score_transform(
                    self.reference,
                    self.candidate,
                    transform,
                    self._structural_penalty,
                ),
            )
            key = (
                score,
                outer_rms,
                hole_rms,
                int(transform.reflected),
                int(transform.reversed),
                transform.cyclic_shift,
            )
            if self._best_key is None or key < self._best_key:
                self._best_key = key
                self._best_score = score
                self._best_outer_rms = outer_rms
                self._best_hole_rms = hole_rms
                self._best_transform = transform
            return

        if self.phase == "result":
            if self._best_transform is None:
                self._finish_rejected(
                    "outer_fit_failed",
                    coarse=self._coarse,
                    topology=self._topology,
                )
                return
            score = self._best_score
            accepted = score <= self.tolerance
            if not accepted:
                self._diagnostics.rejected += 1
                self.result = similarity_matcher.MatchResult(
                    accepted=False,
                    score=score,
                    transform=None,
                    reason="score_above_tolerance",
                    outer_rms=self._best_outer_rms,
                    hole_rms=self._best_hole_rms,
                    topology_penalty=self._structural_penalty,
                    coarse_gate=self._coarse,
                    topology_gate=self._topology,
                    diagnostics=self._diagnostics.snapshot(),
                )
            else:
                self._diagnostics.accepted += 1
                self.result = similarity_matcher.MatchResult(
                    accepted=True,
                    score=score,
                    transform=replace(
                        self._best_transform,
                        score=score,
                        rms=self._best_transform.rms,
                    ),
                    reason="accepted",
                    outer_rms=self._best_outer_rms,
                    hole_rms=self._best_hole_rms,
                    topology_penalty=self._structural_penalty,
                    coarse_gate=self._coarse,
                    topology_gate=self._topology,
                    diagnostics=self._diagnostics.snapshot(),
                )
            self._set_phase("done")
            self.done = True
            return

        if self.phase == "done":
            self.done = True
            return

        raise RuntimeError("unknown Pro shape phase: %s" % self.phase)

    def advance(self, operation_budget=256, deadline=None):
        """Advance at most ``operation_budget`` pure operations."""

        if int(operation_budget) <= 0:
            raise ValueError("shape operation budget must be positive")
        if self.done:
            return self.result, 0

        started = time.perf_counter()
        operations = 0
        while not self.done and operations < int(operation_budget):
            if deadline is not None and time.perf_counter() >= float(deadline):
                break
            self._advance_one()
            operations += 1
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.shape_primitive_operations += operations
        self.shape_slices += 1
        self.last_shape_slice_ms = elapsed_ms
        self.last_shape_slice_operations = operations
        self.max_shape_slice_ms = max(self.max_shape_slice_ms, elapsed_ms)
        return self.result, operations

    def cancel(self):
        """Discard partial descriptors/results without producing a match."""

        if self.done:
            return
        self.cancelled = True
        self.done = True
        self.result = None
        self._reference_builder = None
        self._candidate_builder = None
        self.reference = None
        self.candidate = None
        self._reference_points = None
        self._candidate_base = None
        self._oriented = None
        self._best_transform = None

    def run_to_completion(self, operation_budget=256):
        """Drive this exact state synchronously for equivalence tests."""

        while not self.done:
            self.advance(operation_budget=operation_budget)
        return self.result


__all__ = ["ProShapeMatchState", "SHAPE_CALL_LIMIT_MS"]
