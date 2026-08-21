"""Primitive-only shape descriptors and batch results for the MC3B pipeline.

The live Blender side may create :mod:`similarity_matcher` dataclasses, but
the process boundary carries only frozen records made from tuples, strings,
numbers and booleans.  The worker reconstructs the sibling matcher classes
after decoding, so pickle never has to resolve a package class identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib.util
import math
from pathlib import Path
import pickle
from typing import Any, Iterable, Mapping, Optional

try:
    from .pro_process_payload import (
        BatchCacheKey,
        CORRESPONDENCE_MODE_HYBRID,
        ExactOptions,
        FrameSizeError,
        MAX_FRAME_BYTES,
        PairResult,
        PICKLE_PROTOCOL,
        PayloadValidationError,
        SnapshotIdentity,
        normalize_correspondence_mode,
        stable_digest,
    )
except ImportError:  # direct-file worker loading
    from pro_process_payload import (  # type: ignore[no-redef]
        BatchCacheKey,
        CORRESPONDENCE_MODE_HYBRID,
        ExactOptions,
        FrameSizeError,
        MAX_FRAME_BYTES,
        PairResult,
        PICKLE_PROTOCOL,
        PayloadValidationError,
        SnapshotIdentity,
        normalize_correspondence_mode,
        stable_digest,
    )


SHAPE_SCHEMA_VERSION = "uv-gpt-pro-shape-v2"
SHAPE_ALGORITHM_VERSION = "similarity-matcher-v1"
SHAPE_OPERATION = "shape_match_batch"
SHAPE_RESULT_OPERATION = "shape_match_result"
FUSED_OPERATION = "fused_correspondence_batch"
FUSED_RESULT_OPERATION = "fused_correspondence_result"
FUSED_SCHEMA_VERSION = "uv-gpt-pro-fused-v3"
FUSED_ALGORITHM_VERSION = "correspondence-mode-v2"
FUSED_PAIR_WIRE_TAG = "fused-pair-v2"
FUSED_OUTCOME_WIRE_TAG = "fused-outcome-v2"
FRAME_HEADER_BYTES = 79
FRAME_PREFIX_BYTES = 8


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadValidationError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise PayloadValidationError(f"{name} must be finite")
    return result


def _optional_finite(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    return _finite(value, name)


def _optional_nonnegative(value: Any, name: str) -> Optional[float]:
    result = _optional_finite(value, name)
    if result is not None and result < 0.0:
        raise PayloadValidationError(f"{name} must be non-negative")
    return result


def _finite_or_none(value: Any, name: str) -> Optional[float]:
    """Keep rejected matcher sentinels out of the wire schema.

    ``similarity_matcher`` uses ``inf`` for metrics that do not exist after a
    gate rejects a candidate.  The process schema deliberately permits only
    finite numeric metrics, so those sentinel values become ``None`` at the
    adapter boundary and are reconstructed only when a matcher object is
    needed again.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise PayloadValidationError(f"{name} must be finite or None") from exc
    return numeric if math.isfinite(numeric) else None


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PayloadValidationError(f"{name} must be an integer")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise PayloadValidationError(f"{name} must be bool")
    return value


def _text(value: Any, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise PayloadValidationError(f"{name} must be non-empty text")
    return value


def _point(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise PayloadValidationError(f"{name} must be a 2D point")
    return (_finite(value[0], f"{name}.x"), _finite(value[1], f"{name}.y"))


def _points(values: Iterable[Any], name: str) -> tuple[tuple[float, float], ...]:
    return tuple(_point(value, name) for value in values)


def _load_similarity_module() -> Any:
    try:
        import similarity_matcher as module  # type: ignore[no-redef]

        return module
    except ImportError:
        path = Path(__file__).with_name("similarity_matcher.py")
        spec = importlib.util.spec_from_file_location("pro_process_shape_similarity", path)
        if spec is None or spec.loader is None:
            raise PayloadValidationError("cannot load pure similarity matcher")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


@dataclass(frozen=True)
class ShapeOptions:
    match_scale: bool = True
    allow_flipping: bool = False
    tolerance: float = 0.01
    allow_tolerant_topology: bool = True
    use_numpy: Optional[bool] = None

    def __post_init__(self) -> None:
        _bool(self.match_scale, "match_scale")
        _bool(self.allow_flipping, "allow_flipping")
        _finite(self.tolerance, "tolerance")
        if self.tolerance < 0.0:
            raise PayloadValidationError("tolerance must be non-negative")
        _bool(self.allow_tolerant_topology, "allow_tolerant_topology")
        if self.use_numpy is not None:
            _bool(self.use_numpy, "use_numpy")

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.match_scale,
            self.allow_flipping,
            self.tolerance,
            self.allow_tolerant_topology,
            self.use_numpy,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ShapeOptions":
        if not isinstance(value, (tuple, list)) or len(value) != 5:
            raise PayloadValidationError("invalid shape options wire value")
        return cls(*value)


@dataclass(frozen=True)
class TopologyData:
    face_count: Optional[int] = None
    edge_count: Optional[int] = None
    vertex_count: Optional[int] = None
    non_manifold_edge_count: Optional[int] = None
    edge_incidence_histogram: tuple[tuple[int, int], ...] = ()
    component_count: Optional[int] = None
    closed_component_count: Optional[int] = None
    open_component_count: Optional[int] = None
    ambiguous_component_count: Optional[int] = None
    boundary_loop_count: Optional[int] = None
    closed_loop_count: Optional[int] = None
    outer_count: Optional[int] = None
    hole_count: Optional[int] = None
    degenerate_count: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "face_count", "edge_count", "vertex_count", "non_manifold_edge_count",
            "component_count", "closed_component_count", "open_component_count",
            "ambiguous_component_count", "boundary_loop_count", "closed_loop_count",
            "outer_count", "hole_count", "degenerate_count",
        ):
            value = getattr(self, name)
            if value is not None:
                _integer(value, name)
        histogram = tuple(
            (_integer(item[0], "histogram key"), _integer(item[1], "histogram count"))
            for item in self.edge_incidence_histogram
        )
        object.__setattr__(self, "edge_incidence_histogram", histogram)

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.face_count,
            self.edge_count,
            self.vertex_count,
            self.non_manifold_edge_count,
            self.edge_incidence_histogram,
            self.component_count,
            self.closed_component_count,
            self.open_component_count,
            self.ambiguous_component_count,
            self.boundary_loop_count,
            self.closed_loop_count,
            self.outer_count,
            self.hole_count,
            self.degenerate_count,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "TopologyData":
        if not isinstance(value, (tuple, list)) or len(value) != 14:
            raise PayloadValidationError("invalid shape topology wire value")
        return cls(
            face_count=value[0], edge_count=value[1], vertex_count=value[2],
            non_manifold_edge_count=value[3], edge_incidence_histogram=tuple(value[4]),
            component_count=value[5], closed_component_count=value[6],
            open_component_count=value[7], ambiguous_component_count=value[8],
            boundary_loop_count=value[9], closed_loop_count=value[10],
            outer_count=value[11], hole_count=value[12], degenerate_count=value[13],
        )

    @classmethod
    def from_similarity(cls, value: Any) -> "TopologyData":
        return cls(
            face_count=value.face_count,
            edge_count=value.edge_count,
            vertex_count=value.vertex_count,
            non_manifold_edge_count=value.non_manifold_edge_count,
            edge_incidence_histogram=tuple(value.edge_incidence_histogram),
            component_count=value.component_count,
            closed_component_count=value.closed_component_count,
            open_component_count=value.open_component_count,
            ambiguous_component_count=value.ambiguous_component_count,
            boundary_loop_count=value.boundary_loop_count,
            closed_loop_count=value.closed_loop_count,
            outer_count=value.outer_count,
            hole_count=value.hole_count,
            degenerate_count=value.degenerate_count,
        )

    def to_similarity(self, module: Any = None) -> Any:
        module = module or _load_similarity_module()
        return module.TopologySignature(
            face_count=self.face_count,
            edge_count=self.edge_count,
            vertex_count=self.vertex_count,
            non_manifold_edge_count=self.non_manifold_edge_count,
            edge_incidence_histogram=self.edge_incidence_histogram,
            component_count=self.component_count,
            closed_component_count=self.closed_component_count,
            open_component_count=self.open_component_count,
            ambiguous_component_count=self.ambiguous_component_count,
            boundary_loop_count=self.boundary_loop_count,
            closed_loop_count=self.closed_loop_count,
            outer_count=self.outer_count,
            hole_count=self.hole_count,
            degenerate_count=self.degenerate_count,
        )


@dataclass(frozen=True)
class BoundaryLoopData:
    points: tuple[tuple[float, float], ...]
    closed: bool
    status: str
    perimeter: float
    signed_area: float
    area: float
    winding: int
    degenerate: bool
    sample_count: int
    samples: tuple[tuple[float, float], ...]
    role: str = "unclassified"
    containment_depth: int = 0
    parent_outer_index: int = -1
    component_index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", _points(self.points, "loop.points"))
        object.__setattr__(self, "samples", _points(self.samples, "loop.samples"))
        _bool(self.closed, "loop.closed")
        _text(self.status, "loop.status")
        _finite(self.perimeter, "loop.perimeter")
        _finite(self.signed_area, "loop.signed_area")
        _finite(self.area, "loop.area")
        _integer(self.winding, "loop.winding", minimum=-1)
        _bool(self.degenerate, "loop.degenerate")
        _integer(self.sample_count, "loop.sample_count")
        _text(self.role, "loop.role")
        _integer(self.containment_depth, "loop.containment_depth")
        _integer(self.parent_outer_index, "loop.parent_outer_index", minimum=-1)
        _integer(self.component_index, "loop.component_index")

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.points, self.closed, self.status, self.perimeter, self.signed_area,
            self.area, self.winding, self.degenerate, self.sample_count, self.samples,
            self.role, self.containment_depth, self.parent_outer_index, self.component_index,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "BoundaryLoopData":
        if not isinstance(value, (tuple, list)) or len(value) != 14:
            raise PayloadValidationError("invalid shape loop wire value")
        return cls(*value)

    @classmethod
    def from_similarity(cls, value: Any) -> "BoundaryLoopData":
        return cls(
            points=tuple(value.points), closed=value.closed, status=value.status,
            perimeter=value.perimeter, signed_area=value.signed_area, area=value.area,
            winding=value.winding, degenerate=value.degenerate, sample_count=value.sample_count,
            samples=tuple(value.samples), role=value.role,
            containment_depth=value.containment_depth,
            parent_outer_index=value.parent_outer_index,
            component_index=value.component_index,
        )

    def to_similarity(self, module: Any = None) -> Any:
        module = module or _load_similarity_module()
        return module.BoundaryLoop(*self.to_wire())


@dataclass(frozen=True)
class ShapeDescriptor:
    face_key: tuple[Any, ...]
    loops: tuple[BoundaryLoopData, ...]
    outer_loops: tuple[BoundaryLoopData, ...]
    hole_loops: tuple[BoundaryLoopData, ...]
    open_loops: tuple[BoundaryLoopData, ...]
    topology: TopologyData
    bounds: tuple[float, float, float, float]
    center: tuple[float, float]
    boundary_signature: tuple[Any, ...]
    normalized_shape_signature: tuple[Any, ...]
    raw_boundary_signature: tuple[Any, ...] = ()
    descriptor_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "face_key", tuple(self.face_key))
        object.__setattr__(self, "loops", tuple(self.loops))
        object.__setattr__(self, "outer_loops", tuple(self.outer_loops))
        object.__setattr__(self, "hole_loops", tuple(self.hole_loops))
        object.__setattr__(self, "open_loops", tuple(self.open_loops))
        object.__setattr__(self, "bounds", tuple(_finite(item, "descriptor.bounds") for item in self.bounds))
        if len(self.bounds) != 4:
            raise PayloadValidationError("descriptor bounds must contain four values")
        object.__setattr__(self, "center", _point(self.center, "descriptor.center"))
        if not self.descriptor_digest:
            object.__setattr__(self, "descriptor_digest", stable_digest(self._content_wire()))
        _text(self.descriptor_digest, "descriptor_digest")

    def _content_wire(self) -> tuple[Any, ...]:
        return (
            self.face_key,
            tuple(item.to_wire() for item in self.loops),
            tuple(item.to_wire() for item in self.outer_loops),
            tuple(item.to_wire() for item in self.hole_loops),
            tuple(item.to_wire() for item in self.open_loops),
            self.topology.to_wire(), self.bounds, self.center,
            self.boundary_signature, self.normalized_shape_signature,
            self.raw_boundary_signature,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": "shape_descriptor",
            "digest": self.descriptor_digest,
            "content": self._content_wire(),
        }

    @classmethod
    def from_wire(cls, value: Any) -> "ShapeDescriptor":
        if not isinstance(value, Mapping) or value.get("operation") != "shape_descriptor":
            raise PayloadValidationError("invalid shape descriptor wire value")
        content = value.get("content")
        if not isinstance(content, (tuple, list)) or len(content) != 11:
            raise PayloadValidationError("invalid shape descriptor content")
        descriptor = cls(
            face_key=tuple(content[0]),
            loops=tuple(BoundaryLoopData.from_wire(item) for item in content[1]),
            outer_loops=tuple(BoundaryLoopData.from_wire(item) for item in content[2]),
            hole_loops=tuple(BoundaryLoopData.from_wire(item) for item in content[3]),
            open_loops=tuple(BoundaryLoopData.from_wire(item) for item in content[4]),
            topology=TopologyData.from_wire(content[5]),
            bounds=tuple(content[6]), center=tuple(content[7]),
            boundary_signature=tuple(content[8]),
            normalized_shape_signature=tuple(content[9]),
            raw_boundary_signature=tuple(content[10]),
            descriptor_digest=value.get("digest", ""),
        )
        if descriptor.descriptor_digest != stable_digest(descriptor._content_wire()):
            raise PayloadValidationError("shape descriptor digest mismatch")
        return descriptor

    @classmethod
    def from_similarity(cls, value: Any) -> "ShapeDescriptor":
        return cls(
            face_key=tuple(value.face_key),
            loops=tuple(BoundaryLoopData.from_similarity(item) for item in value.loops),
            outer_loops=tuple(BoundaryLoopData.from_similarity(item) for item in value.outer_loops),
            hole_loops=tuple(BoundaryLoopData.from_similarity(item) for item in value.hole_loops),
            open_loops=tuple(BoundaryLoopData.from_similarity(item) for item in value.open_loops),
            topology=TopologyData.from_similarity(value.topology),
            bounds=tuple(value.bounds), center=tuple(value.center),
            boundary_signature=tuple(value.boundary_signature),
            normalized_shape_signature=tuple(value.normalized_shape_signature),
            raw_boundary_signature=tuple(value.raw_boundary_signature),
        )

    def to_similarity(self, module: Any = None) -> Any:
        module = module or _load_similarity_module()
        return module.IslandDescriptor(
            face_key=self.face_key,
            loops=tuple(item.to_similarity(module) for item in self.loops),
            outer_loops=tuple(item.to_similarity(module) for item in self.outer_loops),
            hole_loops=tuple(item.to_similarity(module) for item in self.hole_loops),
            open_loops=tuple(item.to_similarity(module) for item in self.open_loops),
            topology=self.topology.to_similarity(module), bounds=self.bounds, center=self.center,
            boundary_signature=self.boundary_signature,
            normalized_shape_signature=self.normalized_shape_signature,
            raw_boundary_signature=self.raw_boundary_signature,
        )


@dataclass(frozen=True)
class ShapePairTask:
    pair_ordinal: int
    master_key: Any
    member_key: Any
    master_descriptor_digest: str
    member_descriptor_digest: str
    options: ShapeOptions
    prefilter: Optional["ShapePrefilterData"] = None

    def __post_init__(self) -> None:
        _integer(self.pair_ordinal, "pair_ordinal")
        _text(self.master_descriptor_digest, "master_descriptor_digest")
        _text(self.member_descriptor_digest, "member_descriptor_digest")

    def to_wire(self) -> tuple[Any, ...]:
        return (
            "shape-pair", self.pair_ordinal, self.master_key, self.member_key,
            self.master_descriptor_digest, self.member_descriptor_digest,
            self.options.to_wire(),
            None if self.prefilter is None else self.prefilter.to_wire(),
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ShapePairTask":
        if not isinstance(value, (tuple, list)) or len(value) not in (7, 8) or value[0] != "shape-pair":
            raise PayloadValidationError("invalid shape pair wire value")
        return cls(
            pair_ordinal=value[1], master_key=value[2], member_key=value[3],
            master_descriptor_digest=value[4], member_descriptor_digest=value[5],
            options=ShapeOptions.from_wire(value[6]),
            prefilter=(
                None
                if len(value) == 7 or value[7] is None
                else ShapePrefilterData.from_wire(value[7])
            ),
        )


@dataclass(frozen=True)
class ShapeBatchTask:
    identity: SnapshotIdentity
    batch_id: str
    pair_tasks: tuple[ShapePairTask, ...]
    descriptors: tuple[ShapeDescriptor, ...]
    debug_delay_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("shape batch identity is invalid")
        _text(self.batch_id, "batch_id")
        if not self.pair_tasks:
            raise PayloadValidationError("shape batch must contain a pair")
        ordered = tuple(sorted(self.pair_tasks, key=lambda item: item.pair_ordinal))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PayloadValidationError("shape batch has duplicate ordinal")
        object.__setattr__(self, "pair_tasks", ordered)
        object.__setattr__(self, "descriptors", tuple(sorted(self.descriptors, key=lambda item: item.descriptor_digest)))
        _integer(self.debug_delay_ms, "debug_delay_ms")
        if self.debug_delay_ms > 10000:
            raise PayloadValidationError("debug_delay_ms is too large")

    @property
    def pair_ordinals(self) -> tuple[int, ...]:
        return tuple(item.pair_ordinal for item in self.pair_tasks)

    @property
    def descriptor_map(self) -> dict[str, ShapeDescriptor]:
        return {item.descriptor_digest: item for item in self.descriptors}

    def validate(self) -> None:
        descriptor_map = self.descriptor_map
        if len(descriptor_map) != len(self.descriptors):
            raise PayloadValidationError("shape batch has duplicate descriptor digest")
        for descriptor in self.descriptors:
            if descriptor.descriptor_digest != stable_digest(descriptor._content_wire()):
                raise PayloadValidationError("shape descriptor content digest mismatch")
        for pair in self.pair_tasks:
            if pair.master_descriptor_digest not in descriptor_map or pair.member_descriptor_digest not in descriptor_map:
                raise PayloadValidationError("shape pair descriptor reference mismatch")
        _canonical_shape(self.to_wire())

    def to_wire(self) -> dict[str, Any]:
        self.validate_without_wire_recursion()
        return {
            "operation": SHAPE_OPERATION,
            "identity": self.identity.to_wire(),
            "batch_id": self.batch_id,
            "pairs": tuple(item.to_wire() for item in self.pair_tasks),
            "descriptors": tuple(item.to_wire() for item in self.descriptors),
            "debug_delay_ms": self.debug_delay_ms,
        }

    def validate_without_wire_recursion(self) -> None:
        descriptor_map = self.descriptor_map
        if len(descriptor_map) != len(self.descriptors):
            raise PayloadValidationError("shape batch has duplicate descriptor digest")
        for pair in self.pair_tasks:
            if pair.master_descriptor_digest not in descriptor_map or pair.member_descriptor_digest not in descriptor_map:
                raise PayloadValidationError("shape pair descriptor reference mismatch")

    @classmethod
    def from_wire(cls, value: Any) -> "ShapeBatchTask":
        if not isinstance(value, Mapping) or value.get("operation") != SHAPE_OPERATION:
            raise PayloadValidationError("invalid shape batch operation")
        task = cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            batch_id=value["batch_id"],
            pair_tasks=tuple(ShapePairTask.from_wire(item) for item in value["pairs"]),
            descriptors=tuple(ShapeDescriptor.from_wire(item) for item in value["descriptors"]),
            debug_delay_ms=value.get("debug_delay_ms", 0),
        )
        task.validate()
        return task

    def payload_digest(self) -> str:
        return stable_digest(self.to_wire())

    def cache_key(self) -> BatchCacheKey:
        return BatchCacheKey(
            schema_version=SHAPE_SCHEMA_VERSION,
            algorithm_version=SHAPE_ALGORITHM_VERSION,
            generation=self.identity.generation,
            snapshot_digest=self.identity.snapshot_digest,
            pair_digest=stable_digest((
                tuple(item.to_wire() for item in self.pair_tasks),
                tuple(item.descriptor_digest for item in self.descriptors),
            )),
        )

    def estimate_frame(self) -> "ShapeSerializationEstimate":
        return estimate_shape_frame(self)

    @staticmethod
    def result_from_wire(value: Any) -> "ShapeBatchResult":
        return ShapeBatchResult.from_wire(value)


@dataclass(frozen=True)
class ShapeGateData:
    passed: bool
    strict: bool
    penalty: float = 0.0
    reason: str = "ok"
    mismatches: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bool(self.passed, "gate.passed")
        _bool(self.strict, "gate.strict")
        _finite(self.penalty, "gate.penalty")
        _text(self.reason, "gate.reason")
        object.__setattr__(self, "mismatches", tuple(str(item) for item in self.mismatches))

    def to_wire(self) -> tuple[Any, ...]:
        return (self.passed, self.strict, self.penalty, self.reason, self.mismatches)

    @classmethod
    def from_wire(cls, value: Any) -> "ShapeGateData":
        if not isinstance(value, (tuple, list)) or len(value) != 5:
            raise PayloadValidationError("invalid shape gate wire value")
        return cls(value[0], value[1], value[2], value[3], tuple(value[4]))

    @classmethod
    def from_similarity(cls, value: Any) -> "ShapeGateData":
        return cls(value.passed, value.strict, value.penalty, value.reason, tuple(value.mismatches))

    def to_similarity(self, module: Any = None) -> Any:
        module = module or _load_similarity_module()
        return module.GateResult(self.passed, self.strict, self.penalty, self.reason, self.mismatches)


@dataclass(frozen=True)
class ShapePrefilterData:
    """A complete cheap-gate rejection carried through the shape stage.

    The main process already owns the canonical cheap signatures used by the
    synchronous Pro path.  Keeping the rejection as a task-side immutable
    record means the pipeline still returns one complete result for every
    planner ordinal while the worker avoids repeating a full ordered-loop fit
    that the main-side gate has already proved impossible.
    """

    reason: str
    coarse_gate: ShapeGateData
    topology_gate: ShapeGateData
    topology_penalty: float = 0.0

    def __post_init__(self) -> None:
        _text(self.reason, "prefilter.reason")
        if not isinstance(self.coarse_gate, ShapeGateData):
            raise PayloadValidationError("prefilter coarse gate is invalid")
        if not isinstance(self.topology_gate, ShapeGateData):
            raise PayloadValidationError("prefilter topology gate is invalid")
        _finite(self.topology_penalty, "prefilter.topology_penalty")

    def to_wire(self) -> tuple[Any, ...]:
        return (
            "shape-prefilter",
            self.reason,
            self.coarse_gate.to_wire(),
            self.topology_gate.to_wire(),
            self.topology_penalty,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ShapePrefilterData":
        if not isinstance(value, (tuple, list)) or len(value) != 5 or value[0] != "shape-prefilter":
            raise PayloadValidationError("invalid shape prefilter wire value")
        return cls(
            reason=value[1],
            coarse_gate=ShapeGateData.from_wire(value[2]),
            topology_gate=ShapeGateData.from_wire(value[3]),
            topology_penalty=value[4],
        )


@dataclass(frozen=True)
class ShapeDiagnosticsData:
    cheap_signatures: int = 0
    cheap_cache_hits: int = 0
    cheap_candidates: int = 0
    cheap_topology_candidates: int = 0
    descriptor_builds: int = 0
    cache_hits: int = 0
    candidates_seen: int = 0
    coarse_candidates: int = 0
    topology_candidates: int = 0
    full_fits: int = 0
    accepted: int = 0
    rejected: int = 0
    scheduler_decisions: int = 0
    backend_single: int = 0
    backend_thread: int = 0
    backend_process: int = 0
    phase_timings_ms: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "cheap_signatures", "cheap_cache_hits", "cheap_candidates", "cheap_topology_candidates",
            "descriptor_builds", "cache_hits", "candidates_seen", "coarse_candidates",
            "topology_candidates", "full_fits", "accepted", "rejected", "scheduler_decisions",
            "backend_single", "backend_thread", "backend_process",
        ):
            _integer(getattr(self, name), name)
        object.__setattr__(self, "phase_timings_ms", tuple((str(k), _finite(v, "phase time")) for k, v in self.phase_timings_ms))

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.cheap_signatures, self.cheap_cache_hits, self.cheap_candidates,
            self.cheap_topology_candidates, self.descriptor_builds, self.cache_hits,
            self.candidates_seen, self.coarse_candidates, self.topology_candidates,
            self.full_fits, self.accepted, self.rejected, self.scheduler_decisions,
            self.backend_single, self.backend_thread, self.backend_process,
            self.phase_timings_ms,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ShapeDiagnosticsData":
        if not isinstance(value, (tuple, list)) or len(value) != 17:
            raise PayloadValidationError("invalid shape diagnostics wire value")
        return cls(*value)

    @classmethod
    def from_similarity(cls, value: Any) -> "ShapeDiagnosticsData":
        return cls(
            cheap_signatures=value.cheap_signatures, cheap_cache_hits=value.cheap_cache_hits,
            cheap_candidates=value.cheap_candidates, cheap_topology_candidates=value.cheap_topology_candidates,
            descriptor_builds=value.descriptor_builds, cache_hits=value.cache_hits,
            candidates_seen=value.candidates_seen, coarse_candidates=value.coarse_candidates,
            topology_candidates=value.topology_candidates, full_fits=value.full_fits,
            accepted=value.accepted, rejected=value.rejected, scheduler_decisions=value.scheduler_decisions,
            backend_single=value.backend_single, backend_thread=value.backend_thread,
            backend_process=value.backend_process, phase_timings_ms=tuple(value.phase_timings_ms),
        )

    def to_similarity(self, module: Any = None) -> Any:
        module = module or _load_similarity_module()
        return module.DiagnosticsSnapshot(*self.to_wire())


@dataclass(frozen=True)
class ShapeTransformData:
    angle: float
    scale: float
    reflected: bool
    reference_center: tuple[float, float]
    candidate_center: tuple[float, float]
    score: float
    rms: float
    cyclic_shift: int = 0
    reversed: bool = False

    def __post_init__(self) -> None:
        _finite(self.angle, "transform.angle")
        _finite(self.scale, "transform.scale")
        _bool(self.reflected, "transform.reflected")
        object.__setattr__(self, "reference_center", _point(self.reference_center, "transform.reference_center"))
        object.__setattr__(self, "candidate_center", _point(self.candidate_center, "transform.candidate_center"))
        _finite(self.score, "transform.score")
        _finite(self.rms, "transform.rms")
        _integer(self.cyclic_shift, "transform.cyclic_shift")
        _bool(self.reversed, "transform.reversed")

    def to_wire(self) -> tuple[Any, ...]:
        return (self.angle, self.scale, self.reflected, self.reference_center, self.candidate_center, self.score, self.rms, self.cyclic_shift, self.reversed)

    @classmethod
    def from_wire(cls, value: Any) -> Optional["ShapeTransformData"]:
        if value is None:
            return None
        if not isinstance(value, (tuple, list)) or len(value) != 9:
            raise PayloadValidationError("invalid shape transform wire value")
        return cls(*value)

    @classmethod
    def from_similarity(cls, value: Any) -> "ShapeTransformData":
        return cls(value.angle, value.scale, value.reflected, tuple(value.reference_center), tuple(value.candidate_center), value.score, value.rms, value.cyclic_shift, value.reversed)

    def to_similarity(self, module: Any = None) -> Any:
        module = module or _load_similarity_module()
        return module.SimilarityTransform(*self.to_wire())


@dataclass(frozen=True)
class ShapePairResult:
    pair_ordinal: int
    master_key: Any
    member_key: Any
    master_descriptor_digest: str
    member_descriptor_digest: str
    accepted: bool
    score: Optional[float]
    transform: Optional[ShapeTransformData]
    reason: str
    outer_rms: Optional[float]
    hole_rms: Optional[float]
    topology_penalty: float
    coarse_gate: ShapeGateData
    topology_gate: ShapeGateData
    diagnostics: ShapeDiagnosticsData
    complete: bool = True
    first_outer_min_sse: Optional[float] = None
    first_outer_point_count: int = 0

    def __post_init__(self) -> None:
        _integer(self.pair_ordinal, "pair_ordinal")
        _text(self.master_descriptor_digest, "master_descriptor_digest")
        _text(self.member_descriptor_digest, "member_descriptor_digest")
        _bool(self.accepted, "accepted")
        _optional_finite(self.score, "score")
        _optional_finite(self.outer_rms, "outer_rms")
        _optional_finite(self.hole_rms, "hole_rms")
        _optional_nonnegative(self.first_outer_min_sse, "first_outer_min_sse")
        _integer(self.first_outer_point_count, "first_outer_point_count")
        if self.first_outer_min_sse is None and self.first_outer_point_count:
            raise PayloadValidationError("outer SSE point count lacks a bound")
        if self.first_outer_min_sse is not None and not self.first_outer_point_count:
            raise PayloadValidationError("outer SSE bound lacks a point count")
        _finite(self.topology_penalty, "topology_penalty")
        _text(self.reason, "reason")
        _bool(self.complete, "complete")
        if self.accepted and self.transform is None:
            raise PayloadValidationError("accepted shape result lacks transform")

    @classmethod
    def from_similarity(cls, pair: ShapePairTask, result: Any) -> "ShapePairResult":
        return cls(
            pair_ordinal=pair.pair_ordinal, master_key=pair.master_key, member_key=pair.member_key,
            master_descriptor_digest=pair.master_descriptor_digest,
            member_descriptor_digest=pair.member_descriptor_digest,
            accepted=bool(result.accepted),
            score=_finite_or_none(result.score, "score"),
            transform=None if result.transform is None else ShapeTransformData.from_similarity(result.transform),
            reason=str(result.reason), outer_rms=_finite_or_none(result.outer_rms, "outer_rms"),
            hole_rms=_finite_or_none(result.hole_rms, "hole_rms"),
            topology_penalty=float(result.topology_penalty),
            coarse_gate=ShapeGateData.from_similarity(result.coarse_gate),
            topology_gate=ShapeGateData.from_similarity(result.topology_gate),
            diagnostics=ShapeDiagnosticsData.from_similarity(result.diagnostics),
            first_outer_min_sse=_optional_nonnegative(
                getattr(result, "first_outer_min_sse", None),
                "first_outer_min_sse",
            ),
            first_outer_point_count=getattr(result, "first_outer_point_count", 0),
        )

    @classmethod
    def from_prefilter(cls, pair: ShapePairTask, prefilter: ShapePrefilterData) -> "ShapePairResult":
        """Materialize a complete rejection without invoking the full matcher."""

        return cls(
            pair_ordinal=pair.pair_ordinal,
            master_key=pair.master_key,
            member_key=pair.member_key,
            master_descriptor_digest=pair.master_descriptor_digest,
            member_descriptor_digest=pair.member_descriptor_digest,
            accepted=False,
            score=None,
            transform=None,
            reason=prefilter.reason,
            outer_rms=None,
            hole_rms=None,
            topology_penalty=prefilter.topology_penalty,
            coarse_gate=prefilter.coarse_gate,
            topology_gate=prefilter.topology_gate,
            diagnostics=ShapeDiagnosticsData(rejected=1),
            complete=True,
        )

    def validate(self, pair: ShapePairTask, descriptor_map: Mapping[str, ShapeDescriptor]) -> None:
        if not self.complete:
            raise PayloadValidationError("incomplete shape result is not consumable")
        if (self.pair_ordinal, self.master_key, self.member_key) != (pair.pair_ordinal, pair.master_key, pair.member_key):
            raise PayloadValidationError("shape result identity mismatch")
        if (self.master_descriptor_digest, self.member_descriptor_digest) != (pair.master_descriptor_digest, pair.member_descriptor_digest):
            raise PayloadValidationError("shape result descriptor digest mismatch")
        if pair.master_descriptor_digest not in descriptor_map or pair.member_descriptor_digest not in descriptor_map:
            raise PayloadValidationError("shape result references unknown descriptor")
        if self.accepted and (self.transform is None or self.score is None):
            raise PayloadValidationError("accepted shape result lacks finite transform/score")

    def to_wire(self) -> tuple[Any, ...]:
        return (
            "shape-result", self.pair_ordinal, self.master_key, self.member_key,
            self.master_descriptor_digest, self.member_descriptor_digest, self.accepted,
            self.score, None if self.transform is None else self.transform.to_wire(),
            self.reason, self.outer_rms, self.hole_rms, self.topology_penalty,
            self.coarse_gate.to_wire(), self.topology_gate.to_wire(),
            self.diagnostics.to_wire(), self.complete,
            self.first_outer_min_sse, self.first_outer_point_count,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ShapePairResult":
        if (
            not isinstance(value, (tuple, list))
            or len(value) not in (17, 19)
            or value[0] != "shape-result"
        ):
            raise PayloadValidationError("invalid shape result wire value")
        return cls(
            pair_ordinal=value[1], master_key=value[2], member_key=value[3],
            master_descriptor_digest=value[4], member_descriptor_digest=value[5],
            accepted=value[6], score=value[7], transform=ShapeTransformData.from_wire(value[8]),
            reason=value[9], outer_rms=value[10], hole_rms=value[11], topology_penalty=value[12],
            coarse_gate=ShapeGateData.from_wire(value[13]), topology_gate=ShapeGateData.from_wire(value[14]),
            diagnostics=ShapeDiagnosticsData.from_wire(value[15]), complete=value[16],
            first_outer_min_sse=None if len(value) == 17 else value[17],
            first_outer_point_count=0 if len(value) == 17 else value[18],
        )

    def to_similarity(self, module: Any = None) -> Any:
        module = module or _load_similarity_module()
        result_kwargs = dict(
            accepted=self.accepted,
            score=float("inf") if self.score is None else self.score,
            transform=None if self.transform is None else self.transform.to_similarity(module),
            reason=self.reason,
            outer_rms=float("inf") if self.outer_rms is None else self.outer_rms,
            hole_rms=float("inf") if self.hole_rms is None else self.hole_rms,
            topology_penalty=self.topology_penalty,
            coarse_gate=self.coarse_gate.to_similarity(module),
            topology_gate=self.topology_gate.to_similarity(module),
            diagnostics=self.diagnostics.to_similarity(module),
        )
        try:
            return module.MatchResult(
                **result_kwargs,
                first_outer_min_sse=self.first_outer_min_sse,
                first_outer_point_count=self.first_outer_point_count,
            )
        except TypeError:
            # Keep decoding compatible with an older pure matcher during a
            # rolling worker restart; the new field is optional at the wire
            # boundary and the old matcher simply cannot use the proof.
            return module.MatchResult(**result_kwargs)


@dataclass(frozen=True)
class ShapeBatchResult:
    identity: SnapshotIdentity
    batch_id: str
    payload_digest: str
    pair_results: tuple[ShapePairResult, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("shape result identity is invalid")
        _text(self.batch_id, "batch_id")
        _text(self.payload_digest, "payload_digest")
        _bool(self.complete, "complete")
        ordered = tuple(sorted(self.pair_results, key=lambda item: item.pair_ordinal))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PayloadValidationError("shape result has duplicate ordinal")
        object.__setattr__(self, "pair_results", ordered)

    def validate_against(self, task: ShapeBatchTask) -> None:
        task.validate()
        if not self.complete:
            raise PayloadValidationError("incomplete shape batch result is not consumable")
        if self.identity != task.identity or self.batch_id != task.batch_id:
            raise PayloadValidationError("shape batch result identity mismatch")
        if self.payload_digest != task.payload_digest():
            raise PayloadValidationError("shape batch payload digest mismatch")
        pair_map = {item.pair_ordinal: item for item in task.pair_tasks}
        if tuple(item.pair_ordinal for item in self.pair_results) != task.pair_ordinals:
            raise PayloadValidationError("shape result ordinal coverage mismatch")
        for result in self.pair_results:
            pair = pair_map.get(result.pair_ordinal)
            if pair is None:
                raise PayloadValidationError("shape result has foreign ordinal")
            result.validate(pair, task.descriptor_map)

    def result_digest(self) -> str:
        return stable_digest((self.identity.to_wire(), self.batch_id, self.payload_digest, tuple(item.to_wire() for item in self.pair_results)))

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": SHAPE_RESULT_OPERATION,
            "identity": self.identity.to_wire(),
            "batch_id": self.batch_id,
            "payload_digest": self.payload_digest,
            "pair_results": tuple(item.to_wire() for item in self.pair_results),
            "complete": self.complete,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "ShapeBatchResult":
        if not isinstance(value, Mapping) or value.get("operation") != SHAPE_RESULT_OPERATION:
            raise PayloadValidationError("invalid shape result operation")
        return cls(
            identity=SnapshotIdentity.from_wire(value["identity"]), batch_id=value["batch_id"],
            payload_digest=value["payload_digest"],
            pair_results=tuple(ShapePairResult.from_wire(item) for item in value["pair_results"]),
            complete=value.get("complete", False),
        )


def _normalize_seed_transform(value: Any) -> Optional[tuple[Any, ...]]:
    """Freeze an optional candidate-to-master transform for fused exact work."""

    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if len(value) != 5:
            raise PayloadValidationError("invalid fused seed transform")
        angle, scale, reflected, source_center, target_center = value
    else:
        angle = getattr(value, "angle", None)
        scale = getattr(value, "scale", None)
        reflected = getattr(value, "reflected", False)
        source_center = getattr(value, "source_center", None)
        if source_center is None:
            source_center = getattr(value, "candidate_center", None)
        target_center = getattr(value, "target_center", None)
        if target_center is None:
            target_center = getattr(value, "reference_center", None)
    angle = _finite(angle, "fused seed angle")
    scale = _finite(scale, "fused seed scale")
    if scale <= 0.0 or not isinstance(reflected, bool):
        raise PayloadValidationError("invalid fused seed transform")
    source_center = _point(source_center, "fused seed source center")
    target_center = _point(target_center, "fused seed target center")
    return (angle, scale, reflected, source_center, target_center)


@dataclass(frozen=True)
class FusedPairRef:
    """A compact master-affine pair reference for the fused worker."""

    pair_ordinal: int
    master_key: Any
    member_key: Any
    master_descriptor_digest: str
    member_descriptor_digest: str
    master_loop_keys: tuple[Any, ...]
    member_loop_keys: tuple[Any, ...]
    exact_options: ExactOptions = ExactOptions()
    prefilter: Optional[ShapePrefilterData] = None
    seed_transform: Any = None
    correspondence_mode: str = CORRESPONDENCE_MODE_HYBRID
    legacy_mode_less: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _integer(self.pair_ordinal, "fused pair ordinal")
        _text(self.master_descriptor_digest, "fused master descriptor digest")
        _text(self.member_descriptor_digest, "fused member descriptor digest")
        for name, values in (
            ("master_loop_keys", self.master_loop_keys),
            ("member_loop_keys", self.member_loop_keys),
        ):
            if not isinstance(values, (tuple, list)):
                raise PayloadValidationError(f"{name} must be a sequence")
            ordered = tuple(sorted(tuple(values), key=repr))
            if len(set(map(repr, ordered))) != len(ordered):
                raise PayloadValidationError(f"{name} contains duplicate loop key")
            object.__setattr__(self, name, ordered)
        if not isinstance(self.exact_options, ExactOptions):
            raise PayloadValidationError("fused exact options are invalid")
        if self.prefilter is not None and not isinstance(self.prefilter, ShapePrefilterData):
            raise PayloadValidationError("fused prefilter is invalid")
        object.__setattr__(
            self,
            "correspondence_mode",
            normalize_correspondence_mode(self.correspondence_mode),
        )
        if not isinstance(self.legacy_mode_less, bool):
            raise PayloadValidationError("fused legacy mode marker is invalid")
        if self.legacy_mode_less and self.correspondence_mode != CORRESPONDENCE_MODE_HYBRID:
            raise PayloadValidationError(
                "legacy fused pair wire can only represent HYBRID"
            )
        object.__setattr__(self, "seed_transform", _normalize_seed_transform(self.seed_transform))

    def to_shape_pair(self, shape_options: ShapeOptions) -> ShapePairTask:
        return ShapePairTask(
            pair_ordinal=self.pair_ordinal,
            master_key=self.master_key,
            member_key=self.member_key,
            master_descriptor_digest=self.master_descriptor_digest,
            member_descriptor_digest=self.member_descriptor_digest,
            options=shape_options,
            prefilter=self.prefilter,
        )

    def to_wire(self) -> tuple[Any, ...]:
        values = (
            self.pair_ordinal,
            self.master_key,
            self.member_key,
            self.master_descriptor_digest,
            self.member_descriptor_digest,
            tuple(self.master_loop_keys),
            tuple(self.member_loop_keys),
            self.exact_options.to_wire(),
            None if self.prefilter is None else self.prefilter.to_wire(),
        )
        if self.legacy_mode_less:
            legacy = ("fused-pair",) + values
            return legacy if self.seed_transform is None else legacy + (self.seed_transform,)
        current = (FUSED_PAIR_WIRE_TAG,) + values
        if self.seed_transform is not None:
            current += (self.seed_transform,)
        return current + (self.correspondence_mode,)

    @classmethod
    def from_wire(
        cls,
        value: Any,
        *,
        requested_mode: Optional[str] = None,
    ) -> "FusedPairRef":
        if not isinstance(value, (tuple, list)) or not value:
            raise PayloadValidationError("invalid fused pair wire value")
        requested = (
            None
            if requested_mode is None
            else normalize_correspondence_mode(requested_mode)
        )
        if value[0] == "fused-pair" and len(value) in (10, 11):
            mode = CORRESPONDENCE_MODE_HYBRID
            if requested is not None and requested != CORRESPONDENCE_MODE_HYBRID:
                raise PayloadValidationError(
                    "legacy fused pair wire has no correspondence_mode; explicit non-HYBRID request is unsupported"
                )
            seed_transform = None if len(value) == 10 else value[10]
            legacy_mode_less = True
        elif value[0] == FUSED_PAIR_WIRE_TAG and len(value) in (11, 12):
            if not isinstance(value[-1], str) or not value[-1].strip():
                raise PayloadValidationError(
                    "mode-bearing fused pair wire is missing correspondence_mode"
                )
            mode = normalize_correspondence_mode(value[-1])
            if requested is not None and requested != mode:
                raise PayloadValidationError(
                    "fused pair correspondence mode conflicts with requested mode"
                )
            seed_transform = None if len(value) == 11 else value[10]
            legacy_mode_less = False
        else:
            raise PayloadValidationError("invalid fused pair wire value")
        return cls(
            pair_ordinal=value[1],
            master_key=value[2],
            member_key=value[3],
            master_descriptor_digest=value[4],
            member_descriptor_digest=value[5],
            master_loop_keys=tuple(value[6]),
            member_loop_keys=tuple(value[7]),
            exact_options=ExactOptions.from_wire(value[8]),
            prefilter=None if value[9] is None else ShapePrefilterData.from_wire(value[9]),
            seed_transform=seed_transform,
            correspondence_mode=mode,
            legacy_mode_less=legacy_mode_less,
        )


@dataclass(frozen=True)
class FusedBatchTask:
    """One worker-resident fused prefilter/shape/graph/exact batch."""

    identity: SnapshotIdentity
    context_digest: str
    fused_digest: str
    batch_id: str
    pair_tasks: tuple[FusedPairRef, ...]
    debug_delay_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("fused task identity is invalid")
        _text(self.context_digest, "fused topology context digest")
        _text(self.fused_digest, "fused context digest")
        _text(self.batch_id, "fused batch_id")
        if not self.pair_tasks:
            raise PayloadValidationError("fused batch must contain a pair")
        ordered = tuple(sorted(self.pair_tasks, key=lambda item: item.pair_ordinal))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PayloadValidationError("fused batch contains duplicate ordinal")
        if len({repr(_canonical_shape(item.master_key)) for item in ordered}) != 1:
            raise PayloadValidationError("fused batch must be master-affine")
        if len({item.correspondence_mode for item in ordered}) != 1:
            raise PayloadValidationError(
                "fused batch pairs must share one correspondence mode"
            )
        object.__setattr__(self, "pair_tasks", ordered)
        _integer(self.debug_delay_ms, "fused debug delay")
        if self.debug_delay_ms > 10000:
            raise PayloadValidationError("fused debug delay is too large")

    @property
    def operation_kind(self) -> str:
        return "fused"

    @property
    def pair_ordinals(self) -> tuple[int, ...]:
        return tuple(item.pair_ordinal for item in self.pair_tasks)

    @property
    def item_count(self) -> int:
        return len(self.pair_tasks)

    @property
    def island_keys(self) -> tuple[Any, ...]:
        values = []
        for pair in self.pair_tasks:
            values.extend((pair.master_key, pair.member_key))
        return tuple(values)

    @property
    def master_key(self) -> Any:
        return self.pair_tasks[0].master_key

    @property
    def correspondence_mode(self) -> str:
        return self.pair_tasks[0].correspondence_mode

    @property
    def has_legacy_mode_less_pair(self) -> bool:
        return any(item.legacy_mode_less for item in self.pair_tasks)

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": FUSED_OPERATION,
            "identity": self.identity.to_wire(),
            "context_digest": self.context_digest,
            "fused_digest": self.fused_digest,
            "batch_id": self.batch_id,
            "pairs": tuple(item.to_wire() for item in self.pair_tasks),
            "debug_delay_ms": self.debug_delay_ms,
        }

    @classmethod
    def from_wire(
        cls,
        value: Any,
        *,
        requested_mode: Optional[str] = None,
    ) -> "FusedBatchTask":
        if not isinstance(value, Mapping) or value.get("operation") != FUSED_OPERATION:
            raise PayloadValidationError("invalid fused batch operation")
        task = cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            context_digest=value["context_digest"],
            fused_digest=value["fused_digest"],
            batch_id=value["batch_id"],
            pair_tasks=tuple(
                FusedPairRef.from_wire(item, requested_mode=requested_mode)
                for item in value["pairs"]
            ),
            debug_delay_ms=value.get("debug_delay_ms", 0),
        )
        task.validate(requested_mode=requested_mode)
        return task

    def validate(self, *, requested_mode: Optional[str] = None) -> None:
        if requested_mode is not None:
            requested = normalize_correspondence_mode(requested_mode)
            if requested != self.correspondence_mode:
                raise PayloadValidationError(
                    "fused task correspondence mode conflicts with requested mode"
                )
            if requested != CORRESPONDENCE_MODE_HYBRID and self.has_legacy_mode_less_pair:
                raise PayloadValidationError(
                    "legacy fused pair wire cannot satisfy an explicit non-HYBRID request"
                )
        _canonical_shape(self.to_wire())

    def payload_digest(self) -> str:
        return stable_digest(self.to_wire())

    def cache_key(self) -> BatchCacheKey:
        return BatchCacheKey(
            schema_version=FUSED_SCHEMA_VERSION,
            algorithm_version=FUSED_ALGORITHM_VERSION,
            generation=self.identity.generation,
            snapshot_digest=self.identity.snapshot_digest,
            pair_digest=stable_digest((self.context_digest, self.fused_digest, self.to_wire()["pairs"])),
        )

    def estimate_frame(self) -> ShapeSerializationEstimate:
        self.validate()
        encoded = pickle.dumps(self.to_wire(), protocol=PICKLE_PROTOCOL)
        nonce_bytes = self.identity.session_nonce.encode("utf-8")
        batch_bytes = self.batch_id.encode("utf-8")
        frame_bytes = FRAME_PREFIX_BYTES + FRAME_HEADER_BYTES + len(nonce_bytes) + len(batch_bytes) + len(encoded)
        if len(encoded) > MAX_FRAME_BYTES or frame_bytes > MAX_FRAME_BYTES:
            raise FrameSizeError("serialized fused batch exceeds MAX_FRAME_BYTES")
        return ShapeSerializationEstimate(
            payload_bytes=len(encoded),
            frame_bytes=frame_bytes,
            payload_digest=hashlib.sha256(encoded).hexdigest().upper(),
        )

    @staticmethod
    def result_from_wire(value: Any) -> "FusedBatchResult":
        return FusedBatchResult.from_wire(value)


@dataclass(frozen=True)
class FusedPairOutcome:
    pair_ordinal: int
    shape_result: ShapePairResult
    exact_result: Any = None
    terminal_reason: str = ""
    complete: bool = True
    correspondence_mode: str = CORRESPONDENCE_MODE_HYBRID

    def __post_init__(self) -> None:
        _integer(self.pair_ordinal, "fused outcome ordinal")
        if not isinstance(self.shape_result, ShapePairResult):
            raise PayloadValidationError("fused outcome shape result is invalid")
        _text(self.terminal_reason, "fused terminal reason", nonempty=False)
        _bool(self.complete, "fused outcome complete")
        object.__setattr__(
            self,
            "correspondence_mode",
            normalize_correspondence_mode(self.correspondence_mode),
        )

    def to_wire(self) -> tuple[Any, ...]:
        return (
            FUSED_OUTCOME_WIRE_TAG,
            self.pair_ordinal,
            self.shape_result.to_wire(),
            None if self.exact_result is None else self.exact_result.to_wire(),
            self.terminal_reason,
            self.complete,
            self.correspondence_mode,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "FusedPairOutcome":
        if not isinstance(value, (tuple, list)) or not value:
            raise PayloadValidationError("invalid fused outcome wire value")
        if value[0] == "fused-outcome" and len(value) == 6:
            mode = CORRESPONDENCE_MODE_HYBRID
        elif value[0] == FUSED_OUTCOME_WIRE_TAG and len(value) == 7:
            if not isinstance(value[6], str) or not value[6].strip():
                raise PayloadValidationError(
                    "mode-bearing fused outcome wire is missing correspondence_mode"
                )
            mode = normalize_correspondence_mode(value[6])
        else:
            raise PayloadValidationError("invalid fused outcome wire value")
        return cls(
            pair_ordinal=value[1],
            shape_result=ShapePairResult.from_wire(value[2]),
            exact_result=None if value[3] is None else PairResult.from_wire(value[3]),
            terminal_reason=value[4],
            complete=value[5],
            correspondence_mode=mode,
        )


@dataclass(frozen=True)
class FusedBatchResult:
    identity: SnapshotIdentity
    context_digest: str
    fused_digest: str
    batch_id: str
    payload_digest: str
    outcomes: tuple[FusedPairOutcome, ...]
    complete: bool = True
    graph_cache_builds: int = 0
    graph_cache_hits: int = 0
    graph_compute_ms: float = 0.0
    exact_compute_ms: float = 0.0
    shape_compute_ms: float = 0.0
    shape_cache_hits: int = 0
    lower_bound_checked: int = 0
    lower_bound_rejected: int = 0
    lower_bound_skipped: int = 0
    lower_bound_graph_pairs_avoided: int = 0
    lower_bound_min_ratio: float = 0.0
    lower_bound_max_ratio: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("fused result identity is invalid")
        for name in ("context_digest", "fused_digest", "batch_id", "payload_digest"):
            _text(getattr(self, name), "fused result " + name)
        _bool(self.complete, "fused result complete")
        ordered = tuple(sorted(self.outcomes, key=lambda item: item.pair_ordinal))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PayloadValidationError("fused result contains duplicate ordinal")
        object.__setattr__(self, "outcomes", ordered)
        for name in (
            "graph_cache_builds", "graph_cache_hits", "shape_cache_hits",
            "lower_bound_checked", "lower_bound_rejected", "lower_bound_skipped",
            "lower_bound_graph_pairs_avoided",
        ):
            _integer(getattr(self, name), "fused result " + name)
        for name in (
            "graph_compute_ms", "exact_compute_ms", "shape_compute_ms",
            "lower_bound_min_ratio", "lower_bound_max_ratio",
        ):
            _finite(getattr(self, name), "fused result " + name)
        if self.lower_bound_min_ratio < 0.0 or self.lower_bound_max_ratio < 0.0:
            raise PayloadValidationError("fused lower-bound ratios must be non-negative")

    @property
    def pair_results(self) -> tuple[Any, ...]:
        return tuple(item.exact_result for item in self.outcomes if item.exact_result is not None)

    def validate_against(self, task: FusedBatchTask) -> None:
        task.validate()
        if not self.complete:
            raise PayloadValidationError("incomplete fused result is not consumable")
        if (
            self.identity != task.identity
            or self.context_digest != task.context_digest
            or self.fused_digest != task.fused_digest
            or self.batch_id != task.batch_id
            or self.payload_digest != task.payload_digest()
        ):
            raise PayloadValidationError("fused result identity or digest mismatch")
        if tuple(item.pair_ordinal for item in self.outcomes) != task.pair_ordinals:
            raise PayloadValidationError("fused result ordinal coverage mismatch")
        refs = {item.pair_ordinal: item for item in task.pair_tasks}
        for outcome in self.outcomes:
            ref = refs.get(outcome.pair_ordinal)
            if ref is None or outcome.shape_result.pair_ordinal != ref.pair_ordinal:
                raise PayloadValidationError("fused result has foreign ordinal")
            if (
                outcome.shape_result.master_key != ref.master_key
                or outcome.shape_result.member_key != ref.member_key
                or outcome.shape_result.master_descriptor_digest != ref.master_descriptor_digest
                or outcome.shape_result.member_descriptor_digest != ref.member_descriptor_digest
            ):
                raise PayloadValidationError("fused shape result identity mismatch")
            if outcome.correspondence_mode != ref.correspondence_mode:
                raise PayloadValidationError(
                    "fused outcome correspondence mode mismatch"
                )
            if outcome.exact_result is not None:
                exact = outcome.exact_result
                if not isinstance(exact, PairResult):
                    raise PayloadValidationError("fused exact result has invalid type")
                if exact.correspondence_mode != ref.correspondence_mode:
                    raise PayloadValidationError(
                        "fused exact result correspondence mode mismatch"
                    )
                if (
                    exact.pair_ordinal != ref.pair_ordinal
                    or exact.master_key != ref.master_key
                    or exact.member_key != ref.member_key
                ):
                    raise PayloadValidationError("fused exact result identity mismatch")
                source = set(ref.member_loop_keys)
                target = set(ref.master_loop_keys)
                if exact.accepted:
                    if (
                        {item[0] for item in exact.loop_mapping} != source
                        or {item[1] for item in exact.loop_mapping} != target
                        or len(exact.loop_mapping) != len(source)
                    ):
                        raise PayloadValidationError("fused exact mapping is not a full bijection")
                elif exact.loop_mapping:
                    raise PayloadValidationError("fused rejected exact result has a mapping")

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": FUSED_RESULT_OPERATION,
            "identity": self.identity.to_wire(),
            "context_digest": self.context_digest,
            "fused_digest": self.fused_digest,
            "batch_id": self.batch_id,
            "payload_digest": self.payload_digest,
            "outcomes": tuple(item.to_wire() for item in self.outcomes),
            "complete": self.complete,
            "graph_cache_builds": self.graph_cache_builds,
            "graph_cache_hits": self.graph_cache_hits,
            "graph_compute_ms": self.graph_compute_ms,
            "exact_compute_ms": self.exact_compute_ms,
            "shape_compute_ms": self.shape_compute_ms,
            "shape_cache_hits": self.shape_cache_hits,
            "lower_bound_checked": self.lower_bound_checked,
            "lower_bound_rejected": self.lower_bound_rejected,
            "lower_bound_skipped": self.lower_bound_skipped,
            "lower_bound_graph_pairs_avoided": self.lower_bound_graph_pairs_avoided,
            "lower_bound_min_ratio": self.lower_bound_min_ratio,
            "lower_bound_max_ratio": self.lower_bound_max_ratio,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "FusedBatchResult":
        if not isinstance(value, Mapping) or value.get("operation") != FUSED_RESULT_OPERATION:
            raise PayloadValidationError("invalid fused result operation")
        return cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            context_digest=value["context_digest"],
            fused_digest=value["fused_digest"],
            batch_id=value["batch_id"],
            payload_digest=value["payload_digest"],
            outcomes=tuple(FusedPairOutcome.from_wire(item) for item in value["outcomes"]),
            complete=value.get("complete", False),
            graph_cache_builds=value.get("graph_cache_builds", 0),
            graph_cache_hits=value.get("graph_cache_hits", 0),
            graph_compute_ms=value.get("graph_compute_ms", 0.0),
            exact_compute_ms=value.get("exact_compute_ms", 0.0),
            shape_compute_ms=value.get("shape_compute_ms", 0.0),
            shape_cache_hits=value.get("shape_cache_hits", 0),
            lower_bound_checked=value.get("lower_bound_checked", 0),
            lower_bound_rejected=value.get("lower_bound_rejected", 0),
            lower_bound_skipped=value.get("lower_bound_skipped", 0),
            lower_bound_graph_pairs_avoided=value.get("lower_bound_graph_pairs_avoided", 0),
            lower_bound_min_ratio=value.get("lower_bound_min_ratio", 0.0),
            lower_bound_max_ratio=value.get("lower_bound_max_ratio", 0.0),
        )

    def result_digest(self) -> str:
        return stable_digest((self.identity.to_wire(), self.batch_id, self.payload_digest, tuple(item.to_wire() for item in self.outcomes)))


@dataclass(frozen=True)
class ShapeSerializationEstimate:
    payload_bytes: int
    frame_bytes: int
    payload_digest: str
    protocol: int = PICKLE_PROTOCOL


def _canonical_shape(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _canonical_shape(item)) for key, item in value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_canonical_shape(item) for item in value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PayloadValidationError("non-finite shape value")
        return value.hex()
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise PayloadValidationError("shape wire contains a non-primitive value")


def estimate_shape_frame(task: ShapeBatchTask) -> ShapeSerializationEstimate:
    task.validate_without_wire_recursion()
    payload = pickle.dumps(task.to_wire(), protocol=PICKLE_PROTOCOL)
    nonce_bytes = task.identity.session_nonce.encode("utf-8")
    batch_bytes = task.batch_id.encode("utf-8")
    frame_bytes = FRAME_PREFIX_BYTES + FRAME_HEADER_BYTES + len(nonce_bytes) + len(batch_bytes) + len(payload)
    if len(payload) > MAX_FRAME_BYTES or frame_bytes > MAX_FRAME_BYTES:
        raise FrameSizeError("serialized shape batch exceeds MAX_FRAME_BYTES")
    return ShapeSerializationEstimate(
        payload_bytes=len(payload), frame_bytes=frame_bytes,
        payload_digest=hashlib.sha256(payload).hexdigest().upper(),
    )


def descriptor_to_wire(descriptor: Any) -> ShapeDescriptor:
    return ShapeDescriptor.from_similarity(descriptor)


def descriptor_from_wire(value: Any, similarity_module: Any = None) -> Any:
    return ShapeDescriptor.from_wire(value).to_similarity(similarity_module)


def make_shape_batch(
    identity: SnapshotIdentity,
    pairs: Iterable[tuple[int, Any, Any, Any, Any]],
    options: ShapeOptions,
    *,
    batch_id: str,
    debug_delay_ms: int = 0,
) -> ShapeBatchTask:
    """Build one shape batch from ``(ordinal, master, member, ref, cand)`` records."""

    descriptors: dict[str, ShapeDescriptor] = {}
    tasks: list[ShapePairTask] = []
    for item in pairs:
        if not isinstance(item, (tuple, list)) or len(item) not in (5, 6):
            raise PayloadValidationError("shape pair input must contain five or six fields")
        ordinal, master_key, member_key, master, member = item[:5]
        prefilter = item[5] if len(item) == 6 else None
        if prefilter is not None and not isinstance(prefilter, ShapePrefilterData):
            raise PayloadValidationError("shape pair prefilter is invalid")
        master_data = master if isinstance(master, ShapeDescriptor) else descriptor_to_wire(master)
        member_data = member if isinstance(member, ShapeDescriptor) else descriptor_to_wire(member)
        descriptors[master_data.descriptor_digest] = master_data
        descriptors[member_data.descriptor_digest] = member_data
        tasks.append(ShapePairTask(
            pair_ordinal=int(ordinal), master_key=tuple(master_key), member_key=tuple(member_key),
            master_descriptor_digest=master_data.descriptor_digest,
            member_descriptor_digest=member_data.descriptor_digest,
            options=options,
            prefilter=prefilter,
        ))
    task = ShapeBatchTask(identity, batch_id, tuple(tasks), tuple(descriptors.values()), debug_delay_ms)
    task.validate()
    estimate_shape_frame(task)
    return task


__all__ = [
    "SHAPE_SCHEMA_VERSION", "SHAPE_ALGORITHM_VERSION", "SHAPE_OPERATION", "SHAPE_RESULT_OPERATION",
    "FUSED_OPERATION", "FUSED_RESULT_OPERATION", "FUSED_SCHEMA_VERSION",
    "FUSED_ALGORITHM_VERSION", "FUSED_PAIR_WIRE_TAG", "FUSED_OUTCOME_WIRE_TAG",
    "ShapeOptions", "TopologyData", "BoundaryLoopData", "ShapeDescriptor", "ShapePairTask",
    "ShapeBatchTask", "ShapeGateData", "ShapeDiagnosticsData", "ShapeTransformData",
    "ShapePrefilterData", "ShapePairResult", "ShapeBatchResult", "FusedPairRef", "FusedBatchTask",
    "FusedPairOutcome", "FusedBatchResult", "ShapeSerializationEstimate", "estimate_shape_frame",
    "descriptor_to_wire", "descriptor_from_wire", "make_shape_batch",
]
