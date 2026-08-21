"""Pure-numeric geometry matcher used by Align To Selected.

This module deliberately has no Blender imports.  ``stack_tools`` is responsible
for extracting numeric boundary segments and for applying a returned
``SimilarityTransform`` on the Blender main thread.  The matcher works on
immutable descriptors so a per-operator cache cannot retain BMesh objects.

The matching pipeline is intentionally staged:

* ``extract_ordered_boundary_loops`` builds deterministic graph walks;
* ``build_descriptor`` classifies closed rings by containment (not winding
  alone), and prepares bounded arc-length samples;
* ``match_descriptors`` applies a cheap normalized boundary gate, a topology
  gate, then ordered cyclic/reverse Procrustes fitting.

Topology metadata is optional because older callers only have boundary
segments.  Complete raw boundary signatures are a cheap boundary-only gate;
topology core mismatches are then represented by a dimensionless penalty when
the caller opts into ``allow_tolerant_topology``.  Open, ambiguous, degenerate,
outer-count and hole-count mismatches remain hard rejects because a single
whole-island transform cannot safely repair those structures.  With the normal
``0.01`` Similarity Tolerance, the integration uses strict topology filtering;
a deliberately wider tolerance can opt into the core-topology fallback.
"""

from dataclasses import dataclass, field, replace
import itertools
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


try:  # NumPy is optional for Blender 3.6-compatible pure-Python fallback.
    import numpy as _numpy
except Exception:  # pragma: no cover - exercised on installations without NumPy.
    _numpy = None


Point = Tuple[float, float]
EPSILON = 1.0e-9
NODE_EPSILON = 1.0e-7
DEGENERATE_EPSILON = 1.0e-10
MIN_SAMPLE_COUNT = 32
MAX_SAMPLE_COUNT = 128
TOPOLOGY_PENALTY = 0.05
TIE_EPSILON = 1.0e-12
# The fused worker may use this only as a proof-safe negative bound.  The
# relative slack absorbs the two floating-point operations used to square an
# RMS and multiply by its sample count; it is deliberately applied only on the
# rejecting comparison, never to the matcher's semantic score.
OUTER_SSE_BOUND_REL_EPSILON = 1.0e-12
OUTER_SSE_BOUND_ABS_EPSILON = 1.0e-15
_ACTIVE_DIAGNOSTICS = None


def numpy_available() -> bool:
    """Return whether the optional NumPy backend can be selected."""

    return _numpy is not None


def _as_point(point: Any) -> Point:
    """Convert a tuple-like or ``x``/``y`` point to an immutable pair."""

    if hasattr(point, "x") and hasattr(point, "y"):
        return (float(point.x), float(point.y))
    return (float(point[0]), float(point[1]))


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _distance_squared(a: Point, b: Point) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy


def _point_key(point: Point) -> Tuple[int, int]:
    return (round(point[0] / NODE_EPSILON), round(point[1] / NODE_EPSILON))


def _lex_point_key(point: Point) -> Tuple[float, float]:
    return (point[0], point[1])


def _signed_area(points: Sequence[Point]) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        total += point[0] * nxt[1] - nxt[0] * point[1]
    return 0.5 * total


def _perimeter(points: Sequence[Point], closed: bool) -> float:
    if len(points) < 2:
        return 0.0
    total = sum(_distance(a, b) for a, b in zip(points, points[1:]))
    if closed:
        total += _distance(points[-1], points[0])
    return total


def _bounds(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), max(xs), min(ys), max(ys))


def _centroid(points: Sequence[Point]) -> Point:
    if not points:
        return (0.0, 0.0)
    count = float(len(points))
    return (sum(point[0] for point in points) / count, sum(point[1] for point in points) / count)


def _remove_repeated_endpoint(points: Sequence[Point]) -> Tuple[Point, ...]:
    result = tuple(_as_point(point) for point in points)
    if len(result) > 1 and _distance_squared(result[0], result[-1]) <= NODE_EPSILON * NODE_EPSILON:
        result = result[:-1]
    return result


def _canonical_start(points: Sequence[Point], closed: bool) -> Tuple[Point, ...]:
    points = _remove_repeated_endpoint(points)
    if not closed or len(points) < 2:
        return points
    start = min(range(len(points)), key=lambda index: (_lex_point_key(points[index]), index))
    return tuple(points[(start + offset) % len(points)] for offset in range(len(points)))


def _sample_count(point_count: int) -> int:
    if point_count <= 0:
        return 0
    count = MIN_SAMPLE_COUNT
    while count < point_count and count < MAX_SAMPLE_COUNT:
        count *= 2
    return min(MAX_SAMPLE_COUNT, count)


def resample_polyline(
    points: Sequence[Any],
    count: int,
    closed: bool = True,
    canonicalize: bool = True,
) -> Tuple[Point, ...]:
    """Uniformly resample a polyline by arclength.

    Closed output contains exactly ``count`` points and never duplicates the
    first point.  Open output includes both endpoints.  Degenerate input is
    represented by repeated copies of its only usable point, which the
    descriptor marks as unsupported rather than matching silently.
    """

    source = _canonical_start(points, closed) if canonicalize else _remove_repeated_endpoint(points)
    count = max(0, int(count))
    if count == 0 or not source:
        return ()
    if len(source) == 1:
        return tuple(source[0] for _ in range(count))

    segment_lengths = [_distance(a, b) for a, b in zip(source, source[1:])]
    if closed:
        segment_lengths.append(_distance(source[-1], source[0]))
    total = sum(segment_lengths)
    if total <= DEGENERATE_EPSILON:
        return tuple(source[0] for _ in range(count))

    if closed:
        distances = [total * index / count for index in range(count)]
    else:
        distances = [total * index / max(count - 1, 1) for index in range(count)]

    result = []
    segment_index = 0
    segment_start_distance = 0.0
    for distance in distances:
        while (
            segment_index < len(segment_lengths) - 1
            and distance > segment_start_distance + segment_lengths[segment_index]
        ):
            segment_start_distance += segment_lengths[segment_index]
            segment_index += 1
        segment_length = segment_lengths[segment_index]
        start = source[segment_index]
        end = source[(segment_index + 1) % len(source)] if closed or segment_index + 1 < len(source) else source[-1]
        if segment_length <= DEGENERATE_EPSILON:
            result.append(start)
            continue
        ratio = (distance - segment_start_distance) / segment_length
        ratio = max(0.0, min(1.0, ratio))
        result.append((start[0] + (end[0] - start[0]) * ratio, start[1] + (end[1] - start[1]) * ratio))
    return tuple(result)


@dataclass(frozen=True)
class BoundaryLoop:
    """Immutable ordered ring/path extracted from numeric boundary segments."""

    points: Tuple[Point, ...]
    closed: bool
    status: str
    perimeter: float
    signed_area: float
    area: float
    winding: int
    degenerate: bool
    sample_count: int
    samples: Tuple[Point, ...]
    role: str = "unclassified"
    containment_depth: int = 0
    parent_outer_index: int = -1
    component_index: int = 0

    @property
    def is_open(self) -> bool:
        return not self.closed or self.status in {"open", "ambiguous"}

    @property
    def supported(self) -> bool:
        return self.closed and self.status == "closed" and not self.degenerate and bool(self.samples)

    def resampled(self, count: Optional[int] = None) -> Tuple[Point, ...]:
        requested = self.sample_count if count is None else int(count)
        return resample_polyline(self.points, requested, closed=self.closed)


@dataclass(frozen=True)
class TopologySignature:
    """Numeric topology metadata; ``None`` means that a field is unavailable."""

    face_count: Optional[int] = None
    edge_count: Optional[int] = None
    vertex_count: Optional[int] = None
    non_manifold_edge_count: Optional[int] = None
    edge_incidence_histogram: Tuple[Tuple[int, int], ...] = ()
    component_count: Optional[int] = None
    closed_component_count: Optional[int] = None
    open_component_count: Optional[int] = None
    ambiguous_component_count: Optional[int] = None
    boundary_loop_count: Optional[int] = None
    closed_loop_count: Optional[int] = None
    outer_count: Optional[int] = None
    hole_count: Optional[int] = None
    degenerate_count: Optional[int] = None

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "TopologySignature":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value

        def optional_int(*names: str) -> Optional[int]:
            for name in names:
                if name in value and value[name] is not None:
                    return int(value[name])
            return None

        histogram = value.get("edge_incidence_histogram", value.get("edge_incidence", ()))
        if isinstance(histogram, Mapping):
            histogram = tuple(sorted((int(key), int(count)) for key, count in histogram.items()))
        else:
            histogram = tuple((int(key), int(count)) for key, count in histogram)
        return cls(
            face_count=optional_int("face_count", "faces"),
            edge_count=optional_int("edge_count", "edges"),
            vertex_count=optional_int("vertex_count", "vertices"),
            non_manifold_edge_count=optional_int("non_manifold_edge_count", "non_manifold_edges"),
            edge_incidence_histogram=histogram,
        )

    def with_boundary_state(self, loops: Sequence[BoundaryLoop]) -> "TopologySignature":
        closed = sum(1 for loop in loops if loop.status == "closed" and not loop.degenerate)
        open_count = sum(1 for loop in loops if loop.status == "open")
        ambiguous = sum(1 for loop in loops if loop.status == "ambiguous")
        degenerate = sum(1 for loop in loops if loop.degenerate or loop.status == "degenerate")
        return replace(
            self,
            component_count=len(loops),
            closed_component_count=closed,
            open_component_count=open_count,
            ambiguous_component_count=ambiguous,
            boundary_loop_count=len(loops),
            closed_loop_count=closed,
            outer_count=sum(1 for loop in loops if loop.role == "outer"),
            hole_count=sum(1 for loop in loops if loop.role == "hole"),
            degenerate_count=degenerate,
        )

    @property
    def core_key(self) -> Tuple[Any, ...]:
        return (
            self.face_count,
            self.edge_count,
            self.vertex_count,
            self.non_manifold_edge_count,
            self.edge_incidence_histogram,
        )


@dataclass(frozen=True)
class CheapBoundarySignature:
    """One-pass numeric prefilter record without ordered-loop reconstruction."""

    face_key: Tuple[Any, ...]
    topology: TopologySignature
    segment_count: int
    point_count: int
    component_count: int
    closed_component_count: int
    open_component_count: int
    ambiguous_component_count: int
    degenerate_segment_count: int
    cycle_count: int
    perimeter: float
    bounds: Tuple[float, float, float, float]
    center: Point
    invariant_signature: Tuple[float, ...]
    raw_boundary_signature: Tuple[Any, ...]

    @property
    def supported(self) -> bool:
        return (
            self.closed_component_count > 0
            and self.open_component_count == 0
            and self.ambiguous_component_count == 0
            and self.degenerate_segment_count == 0
        )


@dataclass(frozen=True)
class IslandDescriptor:
    """Immutable numeric description of one UV island."""

    face_key: Tuple[Any, ...]
    loops: Tuple[BoundaryLoop, ...]
    outer_loops: Tuple[BoundaryLoop, ...]
    hole_loops: Tuple[BoundaryLoop, ...]
    open_loops: Tuple[BoundaryLoop, ...]
    topology: TopologySignature
    bounds: Tuple[float, float, float, float]
    center: Point
    boundary_signature: Tuple[Any, ...]
    normalized_shape_signature: Tuple[Any, ...]
    raw_boundary_signature: Tuple[Any, ...] = ()

    @property
    def supported(self) -> bool:
        return bool(self.outer_loops) and not self.open_loops and all(loop.supported for loop in self.loops)

    @property
    def perimeter_scale(self) -> float:
        if not self.outer_loops:
            return 0.0
        return max(loop.perimeter for loop in self.outer_loops)


@dataclass(frozen=True)
class SimilarityTransform:
    """Candidate-to-reference 2D similarity transform."""

    angle: float
    scale: float
    reflected: bool
    reference_center: Point
    candidate_center: Point
    score: float
    rms: float
    cyclic_shift: int = 0
    reversed: bool = False

    def apply(self, point: Any) -> Point:
        x, y = _as_point(point)
        x -= self.candidate_center[0]
        y -= self.candidate_center[1]
        if self.reflected:
            x = -x
        cosine = math.cos(self.angle)
        sine = math.sin(self.angle)
        return (
            self.reference_center[0] + self.scale * (x * cosine - y * sine),
            self.reference_center[1] + self.scale * (x * sine + y * cosine),
        )


@dataclass(frozen=True)
class DiagnosticsSnapshot:
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
    phase_timings_ms: Tuple[Tuple[str, float], ...] = ()


@dataclass
class MatcherDiagnostics:
    """Mutable per-run counters; snapshots are immutable and returned in results."""

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
    phase_timings_ms: Dict[str, float] = field(default_factory=dict)

    def record_phase(self, name: str, elapsed_ms: float) -> None:
        key = str(name)
        self.phase_timings_ms[key] = self.phase_timings_ms.get(key, 0.0) + float(elapsed_ms)

    def record_scheduler_decision(self, backend: str) -> None:
        self.scheduler_decisions += 1
        normalized = str(backend).lower()
        if normalized == "single":
            self.backend_single += 1
        elif normalized == "thread":
            self.backend_thread += 1
        elif normalized == "process":
            self.backend_process += 1

    def snapshot(self) -> DiagnosticsSnapshot:
        return DiagnosticsSnapshot(
            cheap_signatures=self.cheap_signatures,
            cheap_cache_hits=self.cheap_cache_hits,
            cheap_candidates=self.cheap_candidates,
            cheap_topology_candidates=self.cheap_topology_candidates,
            descriptor_builds=self.descriptor_builds,
            cache_hits=self.cache_hits,
            candidates_seen=self.candidates_seen,
            coarse_candidates=self.coarse_candidates,
            topology_candidates=self.topology_candidates,
            full_fits=self.full_fits,
            accepted=self.accepted,
            rejected=self.rejected,
            scheduler_decisions=self.scheduler_decisions,
            backend_single=self.backend_single,
            backend_thread=self.backend_thread,
            backend_process=self.backend_process,
            phase_timings_ms=tuple(sorted(self.phase_timings_ms.items())),
        )


def reset_diagnostics() -> DiagnosticsSnapshot:
    """Reset module-level counters for one operator execution.

    This resets counters only.  It never creates or retains a descriptor cache;
    callers that need descriptor reuse must create a ``DescriptorCache`` and
    discard it at the end of the same operator execution.
    """

    global _ACTIVE_DIAGNOSTICS
    _ACTIVE_DIAGNOSTICS = MatcherDiagnostics()
    return _ACTIVE_DIAGNOSTICS.snapshot()


def get_diagnostics() -> DiagnosticsSnapshot:
    """Return an immutable snapshot of the current execution counters."""

    global _ACTIVE_DIAGNOSTICS
    if _ACTIVE_DIAGNOSTICS is None:
        _ACTIVE_DIAGNOSTICS = MatcherDiagnostics()
    return _ACTIVE_DIAGNOSTICS.snapshot()


def record_phase(name: str, elapsed_ms: float) -> None:
    """Record an execution-local phase without exposing mutable diagnostics."""

    _active_diagnostics().record_phase(name, elapsed_ms)


def record_scheduler_decision(backend: str) -> None:
    """Record the backend selected by the execution-local scheduler."""

    _active_diagnostics().record_scheduler_decision(backend)


def record_candidate_stage(stage: str) -> None:
    """Increment a staged candidate counter used by the integration harness."""

    diagnostics = _active_diagnostics()
    field_name = {
        "candidate": "candidates_seen",
        "cheap": "cheap_candidates",
        "cheap_topology": "cheap_topology_candidates",
    }.get(str(stage))
    if field_name is None:
        raise ValueError(f"Unknown candidate diagnostic stage: {stage!r}")
    setattr(diagnostics, field_name, getattr(diagnostics, field_name) + 1)


def record_rejection() -> None:
    """Record a candidate rejected before a full descriptor match."""

    _active_diagnostics().rejected += 1


def _active_diagnostics() -> MatcherDiagnostics:
    global _ACTIVE_DIAGNOSTICS
    if _ACTIVE_DIAGNOSTICS is None:
        _ACTIVE_DIAGNOSTICS = MatcherDiagnostics()
    return _ACTIVE_DIAGNOSTICS


def _freeze_key(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((key, _freeze_key(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_key(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_key(item) for item in value))
    if isinstance(value, float):
        return float(value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def make_descriptor_cache_key(face_key: Iterable[Any], uv_snapshot_identity: Any) -> Tuple[Any, ...]:
    """Build a stable numeric key; callers must pass a per-run UV snapshot id."""

    return (tuple(_freeze_key(item) for item in face_key), _freeze_key(uv_snapshot_identity))


class DescriptorCache:
    """Small per-run cache that stores descriptors, never Blender references."""

    def __init__(self, diagnostics: Optional[MatcherDiagnostics] = None):
        self._entries = {}
        self._cheap_entries = {}
        self.diagnostics = diagnostics or _active_diagnostics()

    def get_or_build_cheap(
        self,
        face_key: Iterable[Any],
        uv_snapshot_identity: Any,
        builder: Callable[[], CheapBoundarySignature],
    ) -> CheapBoundarySignature:
        key = ("cheap", make_descriptor_cache_key(face_key, uv_snapshot_identity))
        signature = self._cheap_entries.get(key)
        if signature is not None:
            self.diagnostics.cheap_cache_hits += 1
            return signature
        signature = builder()
        if not isinstance(signature, CheapBoundarySignature):
            raise TypeError("Cheap signature builders must return CheapBoundarySignature values.")
        self._cheap_entries[key] = signature
        self.diagnostics.cheap_signatures += 1
        return signature

    def get_or_build(
        self,
        face_key: Iterable[Any],
        uv_snapshot_identity: Any,
        builder: Callable[[], IslandDescriptor],
    ) -> IslandDescriptor:
        key = make_descriptor_cache_key(face_key, uv_snapshot_identity)
        descriptor = self._entries.get(key)
        if descriptor is not None:
            self.diagnostics.cache_hits += 1
            return descriptor
        descriptor = builder()
        if not isinstance(descriptor, IslandDescriptor):
            raise TypeError("DescriptorCache builders must return IslandDescriptor values.")
        self._entries[key] = descriptor
        self.diagnostics.descriptor_builds += 1
        return descriptor

    def clear(self) -> None:
        self._entries.clear()
        self._cheap_entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def _edge_other(edge: Tuple[int, int], node: int) -> int:
    return edge[1] if edge[0] == node else edge[0]


def _trace_closed_component(
    nodes: Sequence[Point], edges: Sequence[Tuple[int, int]], adjacency: Mapping[int, Sequence[Tuple[int, int]]], component_nodes: Sequence[int], component_edges: Sequence[int]
) -> Tuple[Tuple[Point, ...], str]:
    start = min(component_nodes, key=lambda index: (_lex_point_key(nodes[index]), index))
    first_choices = sorted(
        adjacency[start],
        key=lambda item: (_lex_point_key(nodes[_edge_other(edges[item[0]], start)]), item[0]),
    )
    if not first_choices:
        return ((nodes[start],), "degenerate")

    current = start
    edge_id = first_choices[0][0]
    used = set()
    points = [nodes[start]]
    for _ in range(len(component_edges) + 1):
        if edge_id in used:
            break
        used.add(edge_id)
        next_node = _edge_other(edges[edge_id], current)
        points.append(nodes[next_node])
        if next_node == start:
            if len(used) == len(component_edges):
                return (_remove_repeated_endpoint(points), "closed")
            break
        choices = [item for item in adjacency[next_node] if item[0] not in used]
        if not choices:
            break
        choices.sort(
            key=lambda item: (_lex_point_key(nodes[_edge_other(edges[item[0]], next_node)]), item[0])
        )
        current = next_node
        edge_id = choices[0][0]
    return (_remove_repeated_endpoint(points), "ambiguous")


def _trace_open_component(
    nodes: Sequence[Point], edges: Sequence[Tuple[int, int]], adjacency: Mapping[int, Sequence[Tuple[int, int]]], component_nodes: Sequence[int], component_edges: Sequence[int]
) -> Tuple[Tuple[Point, ...], str]:
    endpoints = [node for node in component_nodes if len(adjacency[node]) == 1]
    start = min(endpoints or component_nodes, key=lambda index: (_lex_point_key(nodes[index]), index))
    current = start
    previous_edge = None
    used = set()
    points = [nodes[start]]
    while True:
        choices = [item for item in adjacency[current] if item[0] not in used and item[0] != previous_edge]
        if not choices:
            break
        choices.sort(
            key=lambda item: (_lex_point_key(nodes[_edge_other(edges[item[0]], current)]), item[0])
        )
        edge_id = choices[0][0]
        used.add(edge_id)
        next_node = _edge_other(edges[edge_id], current)
        points.append(nodes[next_node])
        previous_edge = edge_id
        current = next_node
    status = "open" if len(used) == len(component_edges) and len(endpoints) == 2 else "ambiguous"
    return (tuple(points), status)


def extract_ordered_boundary_loops(
    segments: Iterable[Tuple[Any, Any]], epsilon: float = NODE_EPSILON
) -> Tuple[BoundaryLoop, ...]:
    """Extract deterministic ordered loops/paths from numeric boundary segments.

    Segment endpoints within ``epsilon`` share one graph node.  Degree-two
    components are closed rings; degree-one components are open paths and
    higher-degree components are marked ambiguous/non-manifold.  Zero-length
    segments become explicit degenerate records so they cannot be mistaken for
    a valid empty boundary.
    """

    # The public epsilon is accepted for caller control, while keys remain
    # deterministic for the default UV precision used by the add-on.
    node_epsilon = max(float(epsilon), 1.0e-12)
    nodes = []
    node_map = {}
    edges = []
    degenerate_points = []

    def node_for(point: Point) -> int:
        # A local quantized key avoids an O(n^2) point search while preserving
        # the first observed numeric coordinate for the immutable descriptor.
        key = (round(point[0] / node_epsilon), round(point[1] / node_epsilon))
        index = node_map.get(key)
        if index is None:
            index = len(nodes)
            node_map[key] = index
            nodes.append(point)
        return index

    for raw_start, raw_end in segments:
        start = _as_point(raw_start)
        end = _as_point(raw_end)
        if _distance(start, end) <= DEGENERATE_EPSILON:
            degenerate_points.append(start)
            continue
        edges.append((node_for(start), node_for(end)))

    adjacency = {index: [] for index in range(len(nodes))}
    for edge_id, (start, end) in enumerate(edges):
        adjacency[start].append((edge_id, end))
        adjacency[end].append((edge_id, start))

    edge_components = []
    visited_edges = set()
    for first_edge in range(len(edges)):
        if first_edge in visited_edges:
            continue
        stack = [first_edge]
        component_edges = []
        component_nodes = set()
        visited_edges.add(first_edge)
        while stack:
            edge_id = stack.pop()
            component_edges.append(edge_id)
            start, end = edges[edge_id]
            component_nodes.update((start, end))
            for node in (start, end):
                for linked_edge, _linked_node in adjacency[node]:
                    if linked_edge not in visited_edges:
                        visited_edges.add(linked_edge)
                        stack.append(linked_edge)
        edge_components.append((sorted(component_nodes), sorted(component_edges)))

    loops = []
    for component_index, (component_nodes, component_edges) in enumerate(edge_components):
        degree = {node: len(adjacency[node]) for node in component_nodes}
        is_degree_two = all(value == 2 for value in degree.values())
        if is_degree_two:
            points, status = _trace_closed_component(nodes, edges, adjacency, component_nodes, component_edges)
        else:
            points, status = _trace_open_component(nodes, edges, adjacency, component_nodes, component_edges)
        points = _canonical_start(points, status == "closed")
        area = _signed_area(points) if status == "closed" else 0.0
        perimeter = _perimeter(points, status == "closed")
        degenerate = len(points) < 3 or perimeter <= DEGENERATE_EPSILON or abs(area) <= DEGENERATE_EPSILON
        sample_count = _sample_count(len(points)) if status == "closed" and not degenerate else 0
        samples = resample_polyline(points, sample_count, closed=True) if sample_count else ()
        loops.append(
            BoundaryLoop(
                points=tuple(points),
                closed=status == "closed",
                status="degenerate" if status == "closed" and degenerate else status,
                perimeter=perimeter,
                signed_area=area,
                area=abs(area),
                winding=1 if area > DEGENERATE_EPSILON else -1 if area < -DEGENERATE_EPSILON else 0,
                degenerate=degenerate,
                sample_count=sample_count,
                samples=tuple(samples),
                component_index=component_index,
            )
        )

    for point in degenerate_points:
        loops.append(
            BoundaryLoop(
                points=(point,),
                closed=False,
                status="degenerate",
                perimeter=0.0,
                signed_area=0.0,
                area=0.0,
                winding=0,
                degenerate=True,
                sample_count=0,
                samples=(),
                component_index=len(loops),
            )
        )

    return tuple(loops)


def _point_in_polygon(point: Point, polygon: Sequence[Point]) -> int:
    """Return 1 inside, 0 outside, 2 on boundary."""

    if len(polygon) < 3:
        return 0
    x, y = point
    inside = False
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        cross = (end[0] - start[0]) * (y - start[1]) - (end[1] - start[1]) * (x - start[0])
        if abs(cross) <= NODE_EPSILON:
            dot = (x - start[0]) * (x - end[0]) + (y - start[1]) * (y - end[1])
            if dot <= NODE_EPSILON * NODE_EPSILON:
                return 2
        if (start[1] > y) != (end[1] > y):
            crossing_x = (end[0] - start[0]) * (y - start[1]) / (end[1] - start[1]) + start[0]
            if x < crossing_x:
                inside = not inside
    return 1 if inside else 0


def _ring_contains(container: BoundaryLoop, inner: BoundaryLoop) -> bool:
    probes = list(inner.points)
    if len(probes) > 8:
        probes = probes[:: max(1, len(probes) // 8)]
    inside = sum(_point_in_polygon(point, container.points) == 1 for point in probes)
    outside = sum(_point_in_polygon(point, container.points) == 0 for point in probes)
    if inside and not outside:
        return True
    if inside > outside and inside >= 2:
        return True
    # A vertex can sit exactly on a container boundary.  Midpoints provide a
    # deterministic second chance without using a centroid (which can be in a
    # concave ring's hole).
    for index, point in enumerate(inner.points):
        nxt = inner.points[(index + 1) % len(inner.points)]
        probe = ((point[0] + nxt[0]) * 0.5, (point[1] + nxt[1]) * 0.5)
        if _point_in_polygon(probe, container.points) == 1:
            return True
    return False


def _classify_loops(loops: Sequence[BoundaryLoop]) -> Tuple[BoundaryLoop, ...]:
    usable = [loop for loop in loops if loop.status == "closed" and not loop.degenerate]
    classified = []
    for loop in loops:
        if loop not in usable:
            classified.append(loop)
            continue
        depth = sum(1 for other in usable if other is not loop and _ring_contains(other, loop))
        classified.append(replace(loop, role="hole" if depth % 2 else "outer", containment_depth=depth))

    outer = [loop for loop in classified if loop.role == "outer"]
    outer.sort(key=lambda loop: (-loop.area, _lex_point_key(loop.points[0]) if loop.points else (0.0, 0.0)))
    outer_indices = {loop: index for index, loop in enumerate(outer)}
    result = []
    for loop in classified:
        if loop.role != "hole":
            result.append(loop)
            continue
        containing = [candidate for candidate in outer if _ring_contains(candidate, loop)]
        parent = min(containing, key=lambda candidate: candidate.area) if containing else None
        result.append(replace(loop, parent_outer_index=outer_indices.get(parent, -1)))
    return tuple(result)


def _shape_signature(loops: Sequence[BoundaryLoop]) -> Tuple[Any, ...]:
    outer = [loop for loop in loops if loop.role == "outer"]
    scale = max((loop.perimeter for loop in outer), default=0.0)
    scale = max(scale, DEGENERATE_EPSILON)
    records = []
    for loop in sorted(loops, key=lambda item: (item.role, item.parent_outer_index, -item.area, item.component_index)):
        if not loop.closed or loop.degenerate:
            records.append((loop.role, loop.status, 0.0, 0.0, 0.0))
            continue
        min_x, max_x, min_y, max_y = _bounds(loop.points)
        width = max_x - min_x
        height = max_y - min_y
        aspect = max(width, height) / max(min(width, height), DEGENERATE_EPSILON)
        records.append(
            (
                loop.role,
                round(loop.perimeter / scale, 8),
                round(loop.area / (scale * scale), 8),
                round(min(aspect, 1.0e6), 8),
                min(MAX_SAMPLE_COUNT, max(0, loop.sample_count)),
            )
        )
    return tuple(records)


def _cheap_component_records(
    segments: Sequence[Tuple[Point, Point]],
) -> Tuple[Dict[str, Any], ...]:
    """Summarize boundary graph connectivity without tracing ordered loops."""

    # The prefilter only needs component cardinality, edge count, and node
    # degree histograms.  A union-find avoids allocating adjacency lists and
    # repeatedly walking every component; ordered graph reconstruction remains
    # deferred to ``build_descriptor`` after the gates pass.
    node_ids: Dict[Tuple[int, int], int] = {}
    parent: List[int] = []
    sizes: List[int] = []
    degrees: List[int] = []
    edges: List[Tuple[int, int]] = []

    def node_id(key: Tuple[int, int]) -> int:
        index = node_ids.get(key)
        if index is not None:
            return index
        index = len(parent)
        node_ids[key] = index
        parent.append(index)
        sizes.append(1)
        degrees.append(0)
        return index

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != index:
            next_index = parent[index]
            parent[index] = root
            index = next_index
        return root

    def union(left: int, right: int) -> None:
        left = find(left)
        right = find(right)
        if left == right:
            return
        if sizes[left] < sizes[right]:
            left, right = right, left
        parent[right] = left
        sizes[left] += sizes[right]

    for start, end in segments:
        start_key = _point_key(start)
        end_key = _point_key(end)
        start_id = node_id(start_key)
        end_id = node_id(end_key)
        if _distance(start, end) <= DEGENERATE_EPSILON:
            continue
        edges.append((start_id, end_id))
        degrees[start_id] += 1
        degrees[end_id] += 1
        union(start_id, end_id)

    components: Dict[int, Dict[str, Any]] = {}
    for index in range(len(parent)):
        root = find(index)
        component = components.setdefault(root, {"nodes": [], "edges": 0})
        component["nodes"].append(degrees[index])
    for start_id, _end_id in edges:
        root = find(start_id)
        components[root]["edges"] += 1

    records = []
    for component in components.values():
        component_degrees = tuple(sorted(component["nodes"]))
        node_count = len(component_degrees)
        edge_count = component["edges"]
        cycle_rank = max(0, edge_count - node_count + 1)
        closed = (
            node_count >= 3
            and edge_count == node_count
            and all(degree == 2 for degree in component_degrees)
        )
        if closed:
            status = "closed"
        elif not edge_count or all(degree <= 1 for degree in component_degrees):
            status = "degenerate"
        elif any(degree > 2 for degree in component_degrees) or cycle_rank > 1:
            status = "ambiguous"
        else:
            status = "open"
        records.append(
            {
                "node_count": node_count,
                "edge_count": edge_count,
                "cycle_rank": cycle_rank,
                "status": status,
                "degree_histogram": tuple(
                    (degree, component_degrees.count(degree))
                    for degree in sorted(set(component_degrees))
                ),
            }
        )
    return tuple(
        sorted(
            records,
            key=lambda item: (
                item["status"],
                item["node_count"],
                item["edge_count"],
                item["cycle_rank"],
                item["degree_histogram"],
            ),
        )
    )


def build_cheap_signature(
    segments: Iterable[Tuple[Any, Any]],
    face_key: Iterable[Any] = (),
    topology: Optional[Mapping[str, Any]] = None,
    include_invariants: bool = True,
) -> CheapBoundarySignature:
    """Build a rotation/scale-invariant prefilter record in one numeric pass.

    This intentionally does not trace ordered boundary loops, classify holes by
    containment, or allocate resampled point arrays.  Those operations belong
    to ``build_descriptor`` and are deferred until this signature passes.
    """

    numeric_segments = tuple(
        (_as_point(start), _as_point(end)) for start, end in segments
    )
    records = _cheap_component_records(numeric_segments)
    points = {
        _point_key(point): point
        for segment in numeric_segments
        for point in segment
    }
    if include_invariants:
        perimeter = sum(_distance(start, end) for start, end in numeric_segments)
        all_points = tuple(points.values())
        center = _centroid(all_points)
        if all_points:
            covariance_xx = sum((point[0] - center[0]) ** 2 for point in all_points) / len(all_points)
            covariance_yy = sum((point[1] - center[1]) ** 2 for point in all_points) / len(all_points)
            covariance_xy = sum(
                (point[0] - center[0]) * (point[1] - center[1]) for point in all_points
            ) / len(all_points)
        else:
            covariance_xx = covariance_yy = covariance_xy = 0.0
        trace = covariance_xx + covariance_yy
        discriminant = math.sqrt(
            max(0.0, (covariance_xx - covariance_yy) ** 2 + 4.0 * covariance_xy**2)
        )
        largest_eigenvalue = max((trace + discriminant) * 0.5, DEGENERATE_EPSILON)
        smallest_eigenvalue = max((trace - discriminant) * 0.5, DEGENERATE_EPSILON)
        invariant_signature = (
            round(largest_eigenvalue / smallest_eigenvalue, 7),
            round(perimeter / math.sqrt(largest_eigenvalue * max(len(all_points), 1)), 7),
        )
    else:
        perimeter = 0.0
        all_points = ()
        center = (0.0, 0.0)
        invariant_signature = ()
    closed_count = sum(item["status"] == "closed" for item in records)
    open_count = sum(item["status"] == "open" for item in records)
    ambiguous_count = sum(item["status"] == "ambiguous" for item in records)
    degenerate_count = sum(item["status"] == "degenerate" for item in records)
    degenerate_segments = sum(
        _distance(start, end) <= DEGENERATE_EPSILON
        for start, end in numeric_segments
    )
    cycle_count = sum(item["cycle_rank"] for item in records)
    base_topology = TopologySignature.from_mapping(topology)
    cheap_topology = replace(
        base_topology,
        component_count=len(records),
        closed_component_count=closed_count,
        open_component_count=open_count,
        ambiguous_component_count=ambiguous_count,
        boundary_loop_count=len(records),
        closed_loop_count=closed_count,
        degenerate_count=degenerate_count,
    )
    raw_signature = (
        len(numeric_segments),
        len(points),
        tuple(
            (
                item["status"],
                item["node_count"],
                item["edge_count"],
                item["cycle_rank"],
            )
            for item in records
        ),
    )
    return CheapBoundarySignature(
        face_key=tuple(_freeze_key(item) for item in face_key),
        topology=cheap_topology,
        segment_count=len(numeric_segments),
        point_count=len(points),
        component_count=len(records),
        closed_component_count=closed_count,
        open_component_count=open_count,
        ambiguous_component_count=ambiguous_count,
        degenerate_segment_count=degenerate_segments,
        cycle_count=cycle_count,
        perimeter=perimeter,
        bounds=_bounds(all_points),
        center=center,
        invariant_signature=invariant_signature,
        raw_boundary_signature=raw_signature,
    )


def build_descriptor(
    segments: Iterable[Tuple[Any, Any]],
    face_key: Iterable[Any] = (),
    topology: Optional[Mapping[str, Any]] = None,
    epsilon: float = NODE_EPSILON,
) -> IslandDescriptor:
    """Build an immutable island descriptor from numeric boundary segments."""

    numeric_segments = tuple(
        (_as_point(start), _as_point(end)) for start, end in segments
    )
    extracted = extract_ordered_boundary_loops(numeric_segments, epsilon=epsilon)
    loops = _classify_loops(extracted)
    outer = tuple(sorted((loop for loop in loops if loop.role == "outer"), key=lambda loop: (-loop.area, loop.component_index)))
    holes = tuple(sorted((loop for loop in loops if loop.role == "hole"), key=lambda loop: (loop.parent_outer_index, -loop.area, loop.component_index)))
    open_loops = tuple(loop for loop in loops if loop.status in {"open", "ambiguous", "degenerate"} or not loop.closed)
    all_points = [point for loop in loops for point in loop.points]
    signature = TopologySignature.from_mapping(topology).with_boundary_state(loops)
    raw_points = {
        _point_key(point)
        for segment in numeric_segments
        for point in segment
        if _distance(*segment) > DEGENERATE_EPSILON
    }
    raw_boundary_signature = (
        len(numeric_segments),
        len(raw_points),
        tuple(
            sorted(
                (loop.status, loop.role, len(loop.points))
                for loop in loops
            )
        ),
    )
    return IslandDescriptor(
        face_key=tuple(_freeze_key(item) for item in face_key),
        loops=tuple(loops),
        outer_loops=outer,
        hole_loops=holes,
        open_loops=open_loops,
        topology=signature,
        bounds=_bounds(all_points),
        center=_centroid(all_points),
        boundary_signature=(
            len(outer),
            len(holes),
            len(open_loops),
            len(loops),
            tuple(loop.sample_count for loop in outer),
        ),
        normalized_shape_signature=_shape_signature(loops),
        raw_boundary_signature=raw_boundary_signature,
    )


describe_island = build_descriptor


@dataclass(frozen=True)
class GateResult:
    passed: bool
    strict: bool
    penalty: float = 0.0
    reason: str = "ok"
    mismatches: Tuple[str, ...] = ()


def cheap_boundary_gate(
    reference: CheapBoundarySignature,
    candidate: CheapBoundarySignature,
) -> GateResult:
    """Compare only one-pass raw boundary counts and invariant moments."""

    if not reference.raw_boundary_signature or not candidate.raw_boundary_signature:
        return GateResult(False, True, reason="missing_boundary_signature")
    if reference.raw_boundary_signature != candidate.raw_boundary_signature:
        return GateResult(False, True, reason="boundary_signature")
    # The coarse stage is deliberately raw-boundary-only.  Optional moment
    # invariants remain available to pure callers, but the operator must not
    # spend time on or reject by rotation/scale moments before topology/full
    # matching has had a chance to apply its documented tolerant semantics.
    if reference.invariant_signature and candidate.invariant_signature:
        for reference_value, candidate_value in zip(
            reference.invariant_signature,
            candidate.invariant_signature,
        ):
            envelope = 0.35 * max(1.0, abs(reference_value), abs(candidate_value))
            if abs(reference_value - candidate_value) > envelope:
                return GateResult(False, True, reason="cheap_invariant_signature")
    return GateResult(True, True)


def cheap_topology_gate(
    reference: CheapBoundarySignature,
    candidate: CheapBoundarySignature,
) -> GateResult:
    """Reject unsafe graph states before any ordered-loop descriptor build."""

    if (
        reference.open_component_count
        or candidate.open_component_count
        or reference.ambiguous_component_count
        or candidate.ambiguous_component_count
    ):
        return GateResult(False, True, reason="unsupported_open_boundary")
    if (
        reference.degenerate_segment_count
        or candidate.degenerate_segment_count
    ):
        return GateResult(False, True, reason="degenerate_boundary")
    if reference.cycle_count != candidate.cycle_count:
        return GateResult(
            False,
            True,
            reason="cycle_count",
            mismatches=("cycle_count",),
        )
    if reference.closed_component_count != candidate.closed_component_count:
        return GateResult(
            False,
            True,
            reason="closed_component_count",
            mismatches=("closed_component_count",),
        )

    mismatches = []
    for name in ("face_count", "edge_count", "vertex_count"):
        reference_value = getattr(reference.topology, name)
        candidate_value = getattr(candidate.topology, name)
        if (
            reference_value is not None
            and candidate_value is not None
            and reference_value != candidate_value
        ):
            mismatches.append(name)
    for name in ("non_manifold_edge_count",):
        reference_value = getattr(reference.topology, name)
        candidate_value = getattr(candidate.topology, name)
        if (
            reference_value is not None
            and candidate_value is not None
            and reference_value != candidate_value
        ):
            mismatches.append(name)
    if (
        reference.topology.edge_incidence_histogram
        and candidate.topology.edge_incidence_histogram
        and reference.topology.edge_incidence_histogram
        != candidate.topology.edge_incidence_histogram
    ):
        mismatches.append("edge_incidence_histogram")
    if not mismatches:
        return GateResult(True, True)
    penalty = TOPOLOGY_PENALTY * min(4.0, max(1.0, len(mismatches) / 2.0))
    return GateResult(
        True,
        False,
        penalty=penalty,
        reason="tolerant_topology",
        mismatches=tuple(mismatches),
    )


def coarse_boundary_gate(reference: IslandDescriptor, candidate: IslandDescriptor) -> GateResult:
    """Cheap scale/rotation-invariant boundary gate before full fitting."""

    if not reference.outer_loops or not candidate.outer_loops:
        return GateResult(False, True, reason="missing_outer_boundary")
    if len(reference.outer_loops) != len(candidate.outer_loops):
        return GateResult(False, True, reason="outer_component_count")
    if bool(reference.open_loops) != bool(candidate.open_loops):
        return GateResult(False, True, reason="open_boundary_state")

    # When the Blender adapter supplies complete topology metadata, preserve
    # the proven MATCH-01 cheap signature using only raw boundary data
    # (segment/point/component counts) before spending time on ordered
    # Procrustes fits.  Mesh topology core mismatches are deliberately left to
    # topology_gate, where they become a documented tolerant penalty rather
    # than being absorbed into this coarse boundary-only stage.
    complete_topology = all(
        value is not None
        for value in (
            reference.topology.face_count,
            reference.topology.edge_count,
            reference.topology.vertex_count,
        )
    ) and bool(reference.raw_boundary_signature and candidate.raw_boundary_signature)
    if complete_topology and reference.raw_boundary_signature != candidate.raw_boundary_signature:
        return GateResult(False, True, reason="boundary_signature")

    ref_signature = reference.normalized_shape_signature
    cand_signature = candidate.normalized_shape_signature
    ref_closed = [item for item in ref_signature if item[0] in {"outer", "hole"}]
    cand_closed = [item for item in cand_signature if item[0] in {"outer", "hole"}]
    if len(ref_closed) != len(cand_closed):
        # Hole count is intentionally left for tolerant topology scoring; the
        # outer shape still gets a chance to fit and receive a penalty.
        ref_closed = [item for item in ref_closed if item[0] == "outer"]
        cand_closed = [item for item in cand_closed if item[0] == "outer"]
    if len(ref_closed) != len(cand_closed):
        return GateResult(False, True, reason="closed_boundary_count")

    # Compare only the scale-normalized perimeter/area/aspect records.  The
    # 0.35 envelope deliberately tolerates triangulation and mild topology
    # variation; ordered fitting remains the correctness gate.
    for ref_item, cand_item in zip(ref_closed, cand_closed):
        for ref_value, cand_value in zip(ref_item[1:4], cand_item[1:4]):
            if abs(float(ref_value) - float(cand_value)) > 0.35:
                return GateResult(False, True, reason="boundary_signature")
    return GateResult(True, True)


def topology_gate(reference: IslandDescriptor, candidate: IslandDescriptor) -> GateResult:
    """Return strict compatibility plus a penalty-bearing tolerant fallback."""

    mismatches = []
    ref_topology = reference.topology
    cand_topology = candidate.topology
    for name in ("face_count", "edge_count", "vertex_count"):
        ref_value = getattr(ref_topology, name)
        cand_value = getattr(cand_topology, name)
        if ref_value is not None and cand_value is not None and ref_value != cand_value:
            mismatches.append(name)

    for name in ("non_manifold_edge_count",):
        ref_value = getattr(ref_topology, name)
        cand_value = getattr(cand_topology, name)
        if ref_value is not None and cand_value is not None and ref_value != cand_value:
            mismatches.append(name)
    if ref_topology.edge_incidence_histogram and cand_topology.edge_incidence_histogram:
        if ref_topology.edge_incidence_histogram != cand_topology.edge_incidence_histogram:
            mismatches.append("edge_incidence_histogram")
    if len(reference.outer_loops) != len(candidate.outer_loops):
        return GateResult(
            False,
            True,
            penalty=TOPOLOGY_PENALTY,
            reason="outer_count",
            mismatches=("outer_count",),
        )
    if len(reference.hole_loops) != len(candidate.hole_loops):
        return GateResult(
            False,
            True,
            penalty=TOPOLOGY_PENALTY,
            reason="hole_count",
            mismatches=("hole_count",),
        )
    if reference.open_loops or candidate.open_loops:
        return GateResult(False, True, reason="unsupported_open_boundary")
    if any(loop.degenerate for loop in reference.loops + candidate.loops):
        return GateResult(False, True, reason="degenerate_boundary")
    if not mismatches:
        return GateResult(True, True)

    penalty = TOPOLOGY_PENALTY * min(4.0, max(1.0, len(mismatches) / 2.0))
    return GateResult(True, False, penalty=penalty, reason="tolerant_topology", mismatches=tuple(mismatches))


def _common_samples(reference: BoundaryLoop, candidate: BoundaryLoop) -> Tuple[Tuple[Point, ...], Tuple[Point, ...]]:
    count = max(reference.sample_count, candidate.sample_count, MIN_SAMPLE_COUNT)
    count = min(MAX_SAMPLE_COUNT, count)
    return (reference.resampled(count), candidate.resampled(count))


def _candidate_sample_sequences(loop: BoundaryLoop, count: int) -> Tuple[Tuple[bool, int, Tuple[Point, ...]], ...]:
    """Return deterministic vertex-started sequences for cyclic fitting.

    A canonical lexicographic start is not similarity-invariant: after a
    rotation or translation a different vertex can become lexicographically
    smallest.  Sampling from every raw vertex therefore handles cyclic starts
    exactly, including different perimeter scales, before the numeric fit.
    """

    base = _canonical_start(loop.points, True)
    sequences = []
    for reversed_order in (False, True):
        oriented = (base[0],) + tuple(reversed(base[1:])) if reversed_order else base
        for start_vertex in range(len(oriented)):
            rotated = oriented[start_vertex:] + oriented[:start_vertex]
            samples = resample_polyline(rotated, count, closed=True, canonicalize=False)
            sequences.append((reversed_order, start_vertex, samples))
    return tuple(sequences)


def _oriented_sequence(points: Sequence[Point], shift: int, reverse: bool) -> Tuple[Point, ...]:
    if reverse:
        base = (points[0],) + tuple(reversed(points[1:]))
    else:
        base = tuple(points)
    count = len(base)
    return tuple(base[(index + shift) % count] for index in range(count))


def _fit_python(
    reference: Sequence[Point], candidate: Sequence[Point], match_scale: bool, reflected: bool
) -> Tuple[float, float, float, Point, Point]:
    ref_center = _centroid(reference)
    cand_center = _centroid(candidate)
    ref_centered = [(point[0] - ref_center[0], point[1] - ref_center[1]) for point in reference]
    cand_centered = [(point[0] - cand_center[0], point[1] - cand_center[1]) for point in candidate]
    if reflected:
        cand_centered = [(-point[0], point[1]) for point in cand_centered]
    a = sum(ref[0] * cand[0] + ref[1] * cand[1] for ref, cand in zip(ref_centered, cand_centered))
    b = sum(ref[1] * cand[0] - ref[0] * cand[1] for ref, cand in zip(ref_centered, cand_centered))
    denominator = sum(point[0] * point[0] + point[1] * point[1] for point in cand_centered)
    angle = math.atan2(b, a) if abs(a) > EPSILON or abs(b) > EPSILON else 0.0
    scale = max(0.0, math.hypot(a, b) / denominator) if match_scale and denominator > EPSILON else 1.0
    cosine = math.cos(angle)
    sine = math.sin(angle)
    error = 0.0
    for ref, cand in zip(ref_centered, cand_centered):
        transformed = (scale * (cand[0] * cosine - cand[1] * sine), scale * (cand[0] * sine + cand[1] * cosine))
        error += _distance_squared(ref, transformed)
    rms = math.sqrt(error / max(len(reference), 1))
    return (angle, scale, rms, ref_center, cand_center)


def _fit_numpy(
    reference: Sequence[Point], candidate: Sequence[Point], match_scale: bool, reflected: bool
) -> Tuple[float, float, float, Point, Point]:
    ref_array = _numpy.asarray(reference, dtype=float)
    cand_array = _numpy.asarray(candidate, dtype=float)
    ref_center_array = ref_array.mean(axis=0)
    cand_center_array = cand_array.mean(axis=0)
    ref_centered = ref_array - ref_center_array
    cand_centered = cand_array - cand_center_array
    if reflected:
        cand_centered = cand_centered.copy()
        cand_centered[:, 0] *= -1.0
    a = float(_numpy.sum(ref_centered[:, 0] * cand_centered[:, 0] + ref_centered[:, 1] * cand_centered[:, 1]))
    b = float(_numpy.sum(ref_centered[:, 1] * cand_centered[:, 0] - ref_centered[:, 0] * cand_centered[:, 1]))
    denominator = float(_numpy.sum(cand_centered * cand_centered))
    angle = math.atan2(b, a) if abs(a) > EPSILON or abs(b) > EPSILON else 0.0
    scale = max(0.0, math.hypot(a, b) / denominator) if match_scale and denominator > EPSILON else 1.0
    cosine = math.cos(angle)
    sine = math.sin(angle)
    transformed = scale * _numpy.column_stack(
        (cand_centered[:, 0] * cosine - cand_centered[:, 1] * sine, cand_centered[:, 0] * sine + cand_centered[:, 1] * cosine)
    )
    error = float(_numpy.sum((ref_centered - transformed) ** 2))
    rms = math.sqrt(error / max(len(reference), 1))
    return (angle, scale, rms, (float(ref_center_array[0]), float(ref_center_array[1])), (float(cand_center_array[0]), float(cand_center_array[1])))


def _fit_one_orientation(
    reference: Sequence[Point], candidate: Sequence[Point], match_scale: bool, reflected: bool, use_numpy: bool
) -> Tuple[float, float, float, Point, Point]:
    if use_numpy and _numpy is not None:
        return _fit_numpy(reference, candidate, match_scale, reflected)
    return _fit_python(reference, candidate, match_scale, reflected)


def _fit_loop_candidates(
    reference: BoundaryLoop,
    candidate: BoundaryLoop,
    match_scale: bool = True,
    allow_flipping: bool = False,
    use_numpy: Optional[bool] = None,
) -> Tuple[SimilarityTransform, ...]:
    """Return ordered fit candidates, preserving deterministic tie order."""

    if not reference.supported or not candidate.supported:
        return ()
    count = min(MAX_SAMPLE_COUNT, max(reference.sample_count, candidate.sample_count, MIN_SAMPLE_COUNT))
    reference_points = reference.resampled(count)
    if len(reference_points) < 3:
        return ()
    use_numpy = numpy_available() if use_numpy is None else bool(use_numpy and numpy_available())
    normalizer = max(reference.perimeter, DEGENERATE_EPSILON)
    candidates = []
    candidate_sequences = _candidate_sample_sequences(candidate, count)
    for reflected in (False, True) if allow_flipping else (False,):
        for reversed_order, start_vertex, oriented in candidate_sequences:
            angle, scale, rms, ref_center, cand_center = _fit_one_orientation(
                reference_points, oriented, match_scale, reflected, use_numpy
            )
            normalized = rms / normalizer
            candidates.append(
                SimilarityTransform(
                    angle=angle,
                    scale=scale,
                    reflected=reflected,
                    reference_center=ref_center,
                    candidate_center=cand_center,
                    score=normalized,
                    rms=rms,
                    cyclic_shift=start_vertex,
                    reversed=reversed_order,
                )
            )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.score,
                int(item.reflected),
                int(item.reversed),
                item.cyclic_shift,
            ),
        )
    )


def _first_outer_min_sse(
    reference: IslandDescriptor,
    candidate: IslandDescriptor,
    fits: Sequence[SimilarityTransform],
) -> Tuple[Optional[float], int]:
    """Return a conservative lower bound from every allowed outer fit.

    The bound is intentionally unavailable for multi-outer or open/ambiguous
    descriptors.  In the valid one-outer case every returned RMS is measured
    over the same ``count`` resampled boundary points, so ``rms**2 * count``
    is the minimum SSE among all cyclic/reverse/reflection fits considered by
    the matcher.  Callers compare it against the full exact-loop SSE budget;
    an unavailable bound must never reject a pair.
    """

    if (
        len(reference.outer_loops) != 1
        or len(candidate.outer_loops) != 1
        or reference.open_loops
        or candidate.open_loops
    ):
        return None, 0
    reference_loop = reference.outer_loops[0]
    candidate_loop = candidate.outer_loops[0]
    if (
        not reference_loop.supported
        or not candidate_loop.supported
        or len(reference_loop.points) != len(candidate_loop.points)
    ):
        return None, 0
    count = min(
        MAX_SAMPLE_COUNT,
        max(reference_loop.sample_count, candidate_loop.sample_count, MIN_SAMPLE_COUNT),
    )
    values = []
    for fit in fits:
        rms = float(fit.rms)
        if math.isfinite(rms) and rms >= 0.0:
            values.append(rms * rms * count)
    if not values:
        return None, 0
    bound = min(values)
    return (bound if math.isfinite(bound) and bound >= 0.0 else None), count


def outer_sse_bound_epsilon(bound: float, budget: float) -> float:
    """Return the conservative arithmetic slack for a strict bound check."""

    return max(
        OUTER_SSE_BOUND_ABS_EPSILON,
        OUTER_SSE_BOUND_REL_EPSILON * max(1.0, abs(float(bound)), abs(float(budget))),
    )


def fit_loop(
    reference: BoundaryLoop,
    candidate: BoundaryLoop,
    match_scale: bool = True,
    allow_flipping: bool = False,
    use_numpy: Optional[bool] = None,
) -> Optional[SimilarityTransform]:
    """Find the best ordered cyclic/reverse/reflection fit for two loops."""

    candidates = _fit_loop_candidates(
        reference,
        candidate,
        match_scale=match_scale,
        allow_flipping=allow_flipping,
        use_numpy=use_numpy,
    )
    return candidates[0] if candidates else None


def _fixed_transform_rms(
    reference: BoundaryLoop, candidate: BoundaryLoop, transform: SimilarityTransform
) -> float:
    count = min(MAX_SAMPLE_COUNT, max(reference.sample_count, candidate.sample_count, MIN_SAMPLE_COUNT))
    reference_points = reference.resampled(count)
    if not reference_points:
        return float("inf")
    best = float("inf")
    for _reversed_order, _start_vertex, oriented in _candidate_sample_sequences(candidate, count):
        error = sum(
            _distance_squared(reference_point, transform.apply(candidate_point))
            for reference_point, candidate_point in zip(reference_points, oriented)
        )
        rms = math.sqrt(error / len(reference_points))
        if rms < best - TIE_EPSILON:
            best = rms
    return best


def _assignment(costs: Sequence[Sequence[float]]) -> Tuple[int, ...]:
    count = len(costs)
    if not count:
        return ()
    if count > 8:
        available = set(range(len(costs[0])))
        result = []
        for row in costs:
            choice = min(available, key=lambda column: (row[column], column)) if available else 0
            result.append(choice)
            available.discard(choice)
        return tuple(result)
    best_cost = None
    best_assignment = None
    for permutation in itertools.permutations(range(len(costs[0])), count):
        total = sum(costs[row][column] for row, column in enumerate(permutation))
        if best_cost is None or total < best_cost - TIE_EPSILON or (
            abs(total - best_cost) <= TIE_EPSILON and permutation < best_assignment
        ):
            best_cost = total
            best_assignment = permutation
    return tuple(best_assignment or ())


@dataclass(frozen=True)
class MatchResult:
    accepted: bool
    score: float
    transform: Optional[SimilarityTransform]
    reason: str
    outer_rms: float
    hole_rms: float
    topology_penalty: float
    coarse_gate: GateResult
    topology_gate: GateResult
    diagnostics: DiagnosticsSnapshot
    # Optional worker-side proof input.  ``None, 0`` means that the descriptor
    # pair was not eligible for the one-outer-boundary theorem.
    first_outer_min_sse: Optional[float] = None
    first_outer_point_count: int = 0


def _score_transform(
    reference: IslandDescriptor,
    candidate: IslandDescriptor,
    transform: SimilarityTransform,
    topology_penalty: float,
) -> Tuple[float, float, float]:
    """Score all rings under one transform; holes disambiguate symmetric outers."""

    normalizer = max(reference.perimeter_scale, DEGENERATE_EPSILON)
    outer_errors = []
    for index, (reference_loop, candidate_loop) in enumerate(
        zip(reference.outer_loops, candidate.outer_loops)
    ):
        # The first outer loop is the loop used by the Kabsch fit, so its RMS
        # is already available on the immutable transform.  Re-walking every
        # cyclic sequence here would turn the bounded fit into an accidental
        # O(raw_vertices^2) operation for every candidate.  Extra disconnected
        # outer loops still receive the fixed-transform verification below.
        if index == 0:
            outer_errors.append(transform.rms)
        else:
            outer_errors.append(
                _fixed_transform_rms(reference_loop, candidate_loop, transform)
            )
    outer_rms = sum(outer_errors) / max(len(outer_errors), 1) / normalizer

    hole_rms = 0.0
    if reference.hole_loops and candidate.hole_loops:
        costs = [
            [
                _fixed_transform_rms(reference_loop, candidate_loop, transform) / normalizer
                for candidate_loop in candidate.hole_loops
            ]
            for reference_loop in reference.hole_loops
        ]
        assignment = _assignment(costs)
        paired = min(len(reference.hole_loops), len(assignment))
        if paired:
            hole_rms = sum(costs[row][assignment[row]] for row in range(paired)) / paired
    score = outer_rms + 0.5 * hole_rms + topology_penalty
    return (score, outer_rms, hole_rms)


def _rejected(
    reason: str,
    diagnostics: MatcherDiagnostics,
    coarse: Optional[GateResult] = None,
    topology: Optional[GateResult] = None,
    score: float = float("inf"),
    topology_penalty: float = 0.0,
) -> MatchResult:
    diagnostics.rejected += 1
    return MatchResult(
        accepted=False,
        score=score,
        transform=None,
        reason=reason,
        outer_rms=float("inf"),
        hole_rms=float("inf"),
        topology_penalty=topology_penalty,
        coarse_gate=coarse or GateResult(False, True, reason=reason),
        topology_gate=topology or GateResult(False, True, reason=reason),
        diagnostics=diagnostics.snapshot(),
    )


def match_descriptors(
    reference: IslandDescriptor,
    candidate: IslandDescriptor,
    match_scale: bool = True,
    allow_flipping: bool = False,
    tolerance: float = 0.01,
    use_numpy: Optional[bool] = None,
    diagnostics: Optional[MatcherDiagnostics] = None,
    allow_tolerant_topology: bool = True,
    count_candidate: bool = True,
) -> MatchResult:
    """Match one candidate descriptor against one selected reference."""

    diagnostics = diagnostics or _active_diagnostics()
    if count_candidate:
        diagnostics.candidates_seen += 1
    coarse = coarse_boundary_gate(reference, candidate)
    if not coarse.passed:
        return _rejected(
            coarse.reason,
            diagnostics,
            coarse=coarse,
            topology_penalty=coarse.penalty,
        )
    diagnostics.coarse_candidates += 1

    topology = topology_gate(reference, candidate)
    if not topology.passed:
        return _rejected(
            topology.reason,
            diagnostics,
            coarse=coarse,
            topology=topology,
            topology_penalty=coarse.penalty + topology.penalty,
        )
    diagnostics.topology_candidates += 1
    if not topology.strict and not allow_tolerant_topology:
        return _rejected(
            "topology_mismatch",
            diagnostics,
            coarse=coarse,
            topology=topology,
            topology_penalty=coarse.penalty + topology.penalty,
        )

    diagnostics.full_fits += 1
    structural_penalty = coarse.penalty + topology.penalty
    fits = _fit_loop_candidates(
        reference.outer_loops[0],
        candidate.outer_loops[0],
        match_scale=match_scale,
        allow_flipping=allow_flipping,
        use_numpy=use_numpy,
    )
    if not fits:
        return _rejected("outer_fit_failed", diagnostics, coarse=coarse, topology=topology)

    first_outer_min_sse, first_outer_point_count = _first_outer_min_sse(
        reference, candidate, fits
    )

    scored = [
        (
            *_score_transform(reference, candidate, fit, structural_penalty),
            int(fit.reflected),
            int(fit.reversed),
            fit.cyclic_shift,
            fit,
        )
        for fit in fits
    ]
    scored.sort(key=lambda item: item[:-1])
    score, outer_rms, hole_rms, _reflected, _reversed, _shift, transform = scored[0]
    accepted = score <= max(0.0, float(tolerance))
    if not accepted:
        diagnostics.rejected += 1
        return MatchResult(
            accepted=False,
            score=score,
            transform=None,
            reason="score_above_tolerance",
            outer_rms=outer_rms,
            hole_rms=hole_rms,
            topology_penalty=structural_penalty,
            coarse_gate=coarse,
            topology_gate=topology,
            diagnostics=diagnostics.snapshot(),
            first_outer_min_sse=first_outer_min_sse,
            first_outer_point_count=first_outer_point_count,
        )
    diagnostics.accepted += 1
    return MatchResult(
        accepted=True,
        score=score,
        transform=replace(transform, score=score, rms=transform.rms),
        reason="accepted",
        outer_rms=outer_rms,
        hole_rms=hole_rms,
        topology_penalty=structural_penalty,
        coarse_gate=coarse,
        topology_gate=topology,
        diagnostics=diagnostics.snapshot(),
        first_outer_min_sse=first_outer_min_sse,
        first_outer_point_count=first_outer_point_count,
    )


def record_match_diagnostics(snapshot: DiagnosticsSnapshot) -> None:
    """Merge one isolated numeric worker snapshot into the main-run counters."""

    diagnostics = _active_diagnostics()
    for name in (
        "coarse_candidates",
        "topology_candidates",
        "full_fits",
        "accepted",
        "rejected",
    ):
        setattr(diagnostics, name, getattr(diagnostics, name) + getattr(snapshot, name))
    for name, elapsed_ms in snapshot.phase_timings_ms:
        diagnostics.record_phase(name, elapsed_ms)


def match_descriptor_task(payload: Tuple[Any, ...]) -> MatchResult:
    """Pickle-safe worker entry point for immutable descriptor matching."""

    (
        reference,
        candidate,
        match_scale,
        allow_flipping,
        tolerance,
        use_numpy,
    ) = payload
    return match_descriptors(
        reference,
        candidate,
        match_scale=bool(match_scale),
        allow_flipping=bool(allow_flipping),
        tolerance=float(tolerance),
        use_numpy=bool(use_numpy),
        diagnostics=MatcherDiagnostics(),
        count_candidate=False,
    )


def match_segments(
    reference_segments: Iterable[Tuple[Any, Any]],
    candidate_segments: Iterable[Tuple[Any, Any]],
    **kwargs: Any,
) -> MatchResult:
    """Convenience API for callers that only have boundary segments."""

    return match_descriptors(build_descriptor(reference_segments), build_descriptor(candidate_segments), **kwargs)


__all__ = [
    "BoundaryLoop",
    "CheapBoundarySignature",
    "DescriptorCache",
    "DiagnosticsSnapshot",
    "GateResult",
    "IslandDescriptor",
    "MatchResult",
    "MatcherDiagnostics",
    "SimilarityTransform",
    "TopologySignature",
    "build_cheap_signature",
    "build_descriptor",
    "cheap_boundary_gate",
    "cheap_topology_gate",
    "coarse_boundary_gate",
    "describe_island",
    "extract_ordered_boundary_loops",
    "fit_loop",
    "get_diagnostics",
    "make_descriptor_cache_key",
    "match_descriptors",
    "match_descriptor_task",
    "match_segments",
    "numpy_available",
    "record_candidate_stage",
    "record_match_diagnostics",
    "record_phase",
    "record_rejection",
    "record_scheduler_decision",
    "resample_polyline",
    "reset_diagnostics",
    "topology_gate",
]
