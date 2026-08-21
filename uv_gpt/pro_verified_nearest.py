"""Pure, conservative verified-nearest correspondence for UV loop graphs.

This module is deliberately independent from Blender.  It is a fast path for
the already shape-compatible island pair selected by the normal-equivalent
matcher.  It never writes UVs and it never replaces the exact correspondence
search.  A fast-path miss is represented by a ``CorrespondenceResult`` whose
``reason`` is always ``"fallback_required"`` and whose mapping is empty.

The nearest phase is only a topology-restricted global assignment.  Candidate
loops are never snapped independently and a master loop can never be used by
two candidates.  The raw candidate UVs are fitted again after the virtual seed
has produced an assignment; only that refit is allowed to produce an accepted
``CorrespondenceResult`` suitable for the existing exact-copy/apply path.
"""

from dataclasses import dataclass, field
import importlib.util
import math
from pathlib import Path
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def _load_topology_module() -> Any:
    """Load the pure topology module in both package and file-test imports."""

    try:
        from . import topology_correspondence as module  # type: ignore

        return module
    except ImportError:
        path = Path(__file__).with_name("topology_correspondence.py")
        resolved_path = str(path.resolve())
        # Focused tests sometimes load both pure modules with independent
        # ``spec_from_file_location`` names.  Reuse the already loaded module
        # when its source path is the same, so CorrespondenceResult identity
        # remains compatible across that import style too.
        for loaded in tuple(sys.modules.values()):
            loaded_path = getattr(loaded, "__file__", None)
            if (
                loaded_path
                and str(Path(loaded_path).resolve()) == resolved_path
                and hasattr(loaded, "CorrespondenceResult")
                and hasattr(loaded, "IslandGraph")
            ):
                return loaded
        name = "_uv_gpt_verified_nearest_topology"
        cached = sys.modules.get(name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load topology_correspondence")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


_topology = _load_topology_module()

Point2 = Tuple[float, float]
LoopKey = Tuple[int, int]

_FALLBACK_REASON_NAMES = (
    "",
    "invalid_graph",
    "missing_transform",
    "topology_mismatch",
    "invalid_seed",
    "seed_apply_failed",
    "empty_domain",
    "domain_empty",
    "symmetric_nearest_tie",
    "ambiguous_assignment",
    "greedy_incomplete_mapping",
    "greedy_topology_verification_failed",
    "assignment_operation_cap",
    "greedy_assignment_failed",
    "incomplete_mapping",
    "scale_or_geometry_fit_failed",
    "reflection_not_allowed",
    "duplicate_nearest_conflict",
    "residual_above_tolerance",
    "invalid_options",
    "multiple_seed_transforms",
    "nonfinite_nearest_distance",
    "seed_re_root_validation_failed",
    "nearest_operation_cap",
)
_FALLBACK_REASON_CODES = {
    name: index for index, name in enumerate(_FALLBACK_REASON_NAMES) if name
}
NEAREST_OPERATION_CAP = 4096


def fallback_reason_code(reason: Any) -> int:
    """Return the stable numeric wire code for a fast-path fallback reason."""

    return int(_FALLBACK_REASON_CODES.get(str(reason or ""), 0))


def fallback_reason_name(code: Any) -> str:
    """Return a stable diagnostic name for a numeric wire code."""

    try:
        index = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if 0 <= index < len(_FALLBACK_REASON_NAMES):
        return _FALLBACK_REASON_NAMES[index] or "none"
    return "unknown_%d" % index


@dataclass(frozen=True, init=False)
class SimilarityTransformSeed:
    """Primitive candidate-to-master transform accepted as a virtual seed.

    ``source_center`` and ``target_center`` intentionally use the same names
    as :class:`topology_correspondence.SimilarityTransform2D`.  The adapter
    also accepts the ``candidate_center``/``reference_center`` names used by
    ``similarity_matcher.SimilarityTransform`` and normalizes them before the
    nearest phase starts.
    """

    angle: float = 0.0
    scale: float = 1.0
    reflected: bool = False
    source_center: Point2 = (0.0, 0.0)
    target_center: Point2 = (0.0, 0.0)

    def __init__(
        self,
        angle: float = 0.0,
        scale: float = 1.0,
        reflected: bool = False,
        source_center: Any = None,
        target_center: Any = None,
        *,
        candidate_center: Any = None,
        reference_center: Any = None,
    ) -> None:
        if source_center is None:
            source_center = candidate_center if candidate_center is not None else (0.0, 0.0)
        if target_center is None:
            target_center = reference_center if reference_center is not None else (0.0, 0.0)
        object.__setattr__(self, "angle", float(angle))
        object.__setattr__(self, "scale", float(scale))
        object.__setattr__(self, "reflected", reflected)
        object.__setattr__(self, "source_center", _point(source_center, "source_center"))
        object.__setattr__(self, "target_center", _point(target_center, "target_center"))

    def apply(self, point: Any) -> Point2:
        x, y = _point(point, "seed point")
        x -= float(self.source_center[0])
        y -= float(self.source_center[1])
        if self.reflected:
            x = -x
        cosine = math.cos(float(self.angle))
        sine = math.sin(float(self.angle))
        return (
            float(self.target_center[0])
            + float(self.scale) * (cosine * x - sine * y),
            float(self.target_center[1])
            + float(self.scale) * (sine * x + cosine * y),
        )

    @property
    def candidate_center(self) -> Point2:
        return self.source_center

    @property
    def reference_center(self) -> Point2:
        return self.target_center


# Names used by callers that describe the same immutable seed contract.
SeedTransform = SimilarityTransformSeed
SimilarityTransform = SimilarityTransformSeed
SimilarityTransform2D = _topology.SimilarityTransform2D
CorrespondenceResult = _topology.CorrespondenceResult
IslandGraph = _topology.IslandGraph


@dataclass(frozen=True)
class VerifiedNearestOptions:
    """Exact options plus bounded pure fast-path controls.

    The four exact fields intentionally mirror ``pro_process_payload`` and
    ``CorrespondenceSearch``.  ``nearest_max_nodes`` only bounds proof work;
    it does not alter or replace the existing exact-search budget.
    """

    allow_flipping: bool = False
    match_scale: bool = True
    tolerance: float = 1.0e-6
    max_search: int = 100000
    nearest_max_nodes: int = NEAREST_OPERATION_CAP
    nearest_tie_epsilon: float = 1.0e-12
    cooperative_yield_every: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.allow_flipping, bool):
            raise ValueError("allow_flipping must be bool")
        if not isinstance(self.match_scale, bool):
            raise ValueError("match_scale must be bool")
        if not math.isfinite(float(self.tolerance)) or float(self.tolerance) < 0.0:
            raise ValueError("tolerance must be finite and nonnegative")
        if int(self.max_search) <= 0:
            raise ValueError("max_search must be positive")
        if int(self.nearest_max_nodes) <= 0:
            raise ValueError("nearest_max_nodes must be positive")
        if (
            not math.isfinite(float(self.nearest_tie_epsilon))
            or float(self.nearest_tie_epsilon) < 0.0
        ):
            raise ValueError("nearest_tie_epsilon must be finite and nonnegative")
        if int(self.cooperative_yield_every) < 0:
            raise ValueError("cooperative_yield_every must be nonnegative")


@dataclass(frozen=True)
class VerifiedNearestDiagnostics(_topology.CorrespondenceDiagnostics):
    """Deterministic nearest evidence carried by the compatible result.

    The elapsed field is explicitly excluded from dataclass equality so this
    evidence can be used in semantic/result digests without timing noise.
    """

    domain_count: int = 0
    domain_candidate_count: int = 0
    distance_evaluations: int = 0
    assignment_nodes: int = 0
    assignment_backtracks: int = 0
    ambiguous: bool = False
    seed_supplied: bool = False
    seed_validated: bool = False
    seed_applied: bool = False
    topology_verified: bool = False
    geometry_verified: bool = False
    max_seed_distance: float = 0.0
    mean_seed_distance: float = 0.0
    ambiguity_count: int = 0
    tie_count: int = 0
    assignment_cap: int = 0
    operations_used: int = 0
    distance_lookups: int = 0
    distance_cache_hits: int = 0
    distance_cache_misses: int = 0
    fallback_reason_code: int = 0
    nearest_elapsed_us: int = field(default=0, compare=False)
    fallback_reason: str = ""

    @property
    def nearest_domain_count(self) -> int:
        return self.domain_count

    @property
    def nearest_domain_candidates(self) -> int:
        return self.domain_candidate_count

    @property
    def nearest_distance_evaluations(self) -> int:
        return self.distance_evaluations

    @property
    def nearest_distance_cache_evaluations(self) -> int:
        """Number of unique compatible-edge distance computations."""

        return self.distance_evaluations

    @property
    def nearest_assignment_nodes(self) -> int:
        return self.assignment_nodes


@dataclass(frozen=True)
class VerifiedNearestResult(_topology.CorrespondenceResult):
    """A ``CorrespondenceResult`` with explicit fast-path/fallback evidence."""

    seed_transform: Optional[Any] = None
    fallback_reason: str = ""
    nearest_diagnostics: VerifiedNearestDiagnostics = VerifiedNearestDiagnostics()

    @property
    def fallback_required(self) -> bool:
        return self.reason == "fallback_required"

    @property
    def requires_fallback(self) -> bool:
        return self.fallback_required


def _point(value: Any, label: str = "point") -> Point2:
    """Copy a finite two-dimensional value without retaining mutable state."""

    try:
        if hasattr(value, "x") and hasattr(value, "y"):
            point = (float(value.x), float(value.y))
        else:
            point = (float(value[0]), float(value[1]))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise ValueError("%s is not a 2D point" % label)
    if not math.isfinite(point[0]) or not math.isfinite(point[1]):
        raise ValueError("%s is not finite" % label)
    return point


def make_seed_transform(
    *,
    angle: float = 0.0,
    scale: float = 1.0,
    reflected: bool = False,
    source_center: Any = (0.0, 0.0),
    target_center: Any = (0.0, 0.0),
    candidate_center: Any = None,
    reference_center: Any = None,
) -> SimilarityTransformSeed:
    """Build an immutable primitive seed using either center naming style."""

    if candidate_center is not None:
        source_center = candidate_center
    if reference_center is not None:
        target_center = reference_center
    return SimilarityTransformSeed(
        angle=float(angle),
        scale=float(scale),
        reflected=reflected,
        source_center=_point(source_center, "source_center"),
        target_center=_point(target_center, "target_center"),
    )


def _field(value: Any, names: Sequence[str], default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def coerce_seed_transform(value: Any) -> Optional[Any]:
    """Normalize a supported seed to immutable ``SimilarityTransform2D``.

    ``None`` means no seed and is valid.  A non-``None`` value that cannot be
    represented by the primitive contract raises ``ValueError``; the main API
    converts that condition to ``fallback_required``.
    """

    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if len(value) != 5:
            raise ValueError("seed_transform_wire_length")
        angle, scale, reflected, source_center, target_center = value
    else:
        angle = _field(value, ("angle",))
        scale = _field(value, ("scale",))
        reflected = _field(value, ("reflected",), False)
        source_center = _field(value, ("source_center", "candidate_center"))
        target_center = _field(value, ("target_center", "reference_center"))
    if angle is None or scale is None or source_center is None or target_center is None:
        raise ValueError("seed_transform_uncomposable")
    if not isinstance(reflected, bool):
        raise ValueError("seed_transform_reflection_flag")
    try:
        angle_value = float(angle)
        scale_value = float(scale)
    except (TypeError, ValueError):
        raise ValueError("seed_transform_numeric")
    if not math.isfinite(angle_value) or not math.isfinite(scale_value):
        raise ValueError("seed_transform_nonfinite")
    if scale_value <= 0.0:
        raise ValueError("seed_transform_nonpositive_scale")
    source = _point(source_center, "seed source_center")
    target = _point(target_center, "seed target_center")
    return _topology.SimilarityTransform2D(
        angle=angle_value,
        scale=scale_value,
        reflected=reflected,
        source_center=source,
        target_center=target,
    )


normalize_seed_transform = coerce_seed_transform


def _graph_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _coerce_graph(value: Any) -> Any:
    """Accept an ``IslandGraph`` or a graph-like immutable record container."""

    if isinstance(value, _topology.IslandGraph):
        return value
    to_graph = getattr(value, "to_topology_graph", None)
    if callable(to_graph):
        try:
            value = to_graph(_topology)
        except TypeError:
            value = to_graph()
        if isinstance(value, _topology.IslandGraph):
            return value
    names = ("faces", "edges", "vertices", "loops")
    records = [_graph_value(value, name, None) for name in names]
    if all(record is not None for record in records):
        boundaries = _graph_value(value, "boundaries", ())
        return _topology.make_graph(
            faces=tuple(records[0]),
            edges=tuple(records[1]),
            vertices=tuple(records[2]),
            loops=tuple(records[3]),
            boundaries=tuple(boundaries or ()),
        )
    loop_values = None
    if isinstance(value, Mapping) and value and all(
        hasattr(item, "key") and hasattr(item, "face_key") for item in value.values()
    ):
        loop_values = tuple(value.values())
    elif isinstance(value, (tuple, list)) and value and all(
        hasattr(item, "key") and hasattr(item, "face_key") for item in value
    ):
        loop_values = tuple(value)
    if loop_values is not None:
        return _graph_from_loop_records(loop_values)
    raise ValueError("graph_uncomposable")


def _graph_from_loop_records(records: Iterable[Any]) -> Any:
    """Conservatively synthesize missing incidence records when safe.

    Boundary component order and hierarchy cannot be inferred from loop-only
    records.  Such inputs are therefore accepted only when they have no
    boundary loops; otherwise the caller receives ``fallback_required``.
    """

    loops = tuple(records)
    if not loops:
        raise ValueError("graph_uncomposable")
    by_face: Dict[Any, List[Any]] = {}
    by_edge: Dict[Any, List[Any]] = {}
    by_vertex: Dict[Any, List[Any]] = {}
    for loop in loops:
        by_face.setdefault(loop.face_key, []).append(loop)
        by_edge.setdefault(loop.edge_key, []).append(loop)
        by_vertex.setdefault(loop.vertex_key, []).append(loop)
    faces = []
    for face_key, face_loops in sorted(by_face.items(), key=lambda item: _stable_key(item[0])):
        keys = {loop.key for loop in face_loops}
        start = min(keys, key=_loop_sort_key)
        ordered = []
        current = start
        while current not in ordered:
            ordered.append(current)
            next_loop = next((item for item in face_loops if item.key == current), None)
            if next_loop is None:
                raise ValueError("graph_face_cycle_missing")
            current = next_loop.next_key
        if current != start or set(ordered) != keys or len(ordered) < 3:
            raise ValueError("graph_face_cycle_ambiguous")
        faces.append(_topology.FaceRecord(face_key, tuple(ordered)))
    edges = []
    for edge_key, edge_loops in sorted(by_edge.items(), key=lambda item: _stable_key(item[0])):
        edge_keys = tuple(sorted((item.key for item in edge_loops), key=_loop_sort_key))
        face_keys = tuple(sorted({item.face_key for item in edge_loops}, key=_stable_key))
        boundary = len(face_keys) == 1
        if any(bool(item.boundary) != boundary for item in edge_loops):
            raise ValueError("graph_edge_boundary_ambiguous")
        edges.append(_topology.EdgeRecord(edge_key, edge_keys, face_keys, boundary))
    vertices = []
    for vertex_key, vertex_loops in sorted(by_vertex.items(), key=lambda item: _stable_key(item[0])):
        keys = tuple(sorted((item.key for item in vertex_loops), key=_loop_sort_key))
        boundary = any(bool(item.boundary) for item in vertex_loops)
        vertices.append(_topology.VertexRecord(vertex_key, keys, boundary))
    boundaries = ()
    boundary_keys = {item.key for item in loops if bool(item.boundary)}
    if boundary_keys:
        # A loop-only single-face polygon has an unambiguous outer component.
        # Multi-face rings need adapter-provided ordered components and are
        # rejected rather than silently weakening hole/component semantics.
        if len(faces) != 1 or boundary_keys != {item.key for item in loops}:
            raise ValueError("graph_boundary_components_missing")
        boundaries = (
            _topology.BoundaryComponentRecord(
                key="outer",
                loop_keys=faces[0].loop_keys,
                role="outer",
            ),
        )
    return _topology.make_graph(faces, edges, vertices, loops, boundaries)


def _stable_key(value: Any) -> Tuple[str, str]:
    helper = getattr(_topology, "_stable_key", None)
    if helper is not None:
        return helper(value)
    return (type(value).__name__, repr(value))


def _loop_sort_key(value: Any) -> Tuple[int, int]:
    helper = getattr(_topology, "_stable_loop_key", None)
    if helper is not None:
        return helper(value)
    return (int(value[0]), int(value[1]))


def _option_value(options: Any, names: Sequence[str], default: Any) -> Any:
    if options is None:
        return default
    if isinstance(options, Mapping):
        for name in names:
            if name in options:
                return options[name]
        return default
    for name in names:
        if hasattr(options, name):
            return getattr(options, name)
    return default


def _resolve_options(
    options: Any,
    *,
    exact_options: Any,
    allow_flipping: Optional[bool],
    match_scale: Optional[bool],
    tolerance: Optional[float],
    max_search: Optional[int],
    nearest_max_nodes: Optional[int],
    nearest_tie_epsilon: Optional[float],
) -> VerifiedNearestOptions:
    source = exact_options if exact_options is not None else options
    return VerifiedNearestOptions(
        allow_flipping=bool(
            allow_flipping
            if allow_flipping is not None
            else _option_value(source, ("allow_flipping",), False)
        ),
        match_scale=bool(
            match_scale
            if match_scale is not None
            else _option_value(source, ("match_scale",), True)
        ),
        tolerance=float(
            tolerance
            if tolerance is not None
            else _option_value(source, ("tolerance",), 1.0e-6)
        ),
        max_search=int(
            max_search
            if max_search is not None
            else _option_value(source, ("max_search",), 100000)
        ),
        nearest_max_nodes=int(
            nearest_max_nodes
            if nearest_max_nodes is not None
            else _option_value(
                source,
                ("nearest_max_nodes", "max_nearest_nodes"),
                NEAREST_OPERATION_CAP,
            )
        ),
        nearest_tie_epsilon=float(
            nearest_tie_epsilon
            if nearest_tie_epsilon is not None
            else _option_value(source, ("nearest_tie_epsilon",), 1.0e-12)
        ),
        cooperative_yield_every=int(
            _option_value(source, ("cooperative_yield_every",), 0)
        ),
    )


def _assign_colors(
    master_signatures: Mapping[LoopKey, Any],
    candidate_signatures: Mapping[LoopKey, Any],
) -> Tuple[Dict[LoopKey, str], Dict[LoopKey, str]]:
    signatures = list(master_signatures.values()) + list(candidate_signatures.values())
    unique = sorted(set(signatures), key=_stable_key)
    colors = {signature: "c%06d" % index for index, signature in enumerate(unique)}
    return (
        {key: colors[value] for key, value in master_signatures.items()},
        {key: colors[value] for key, value in candidate_signatures.items()},
    )


def _refine_labels(
    master: Any, candidate: Any
) -> Tuple[Dict[LoopKey, str], Dict[LoopKey, str], int, bool, bool, int, int]:
    """Mirror the exact engine's capped refinement while retaining evidence."""

    base_builder = getattr(_topology, "_base_loop_signature")
    master_base = {key: base_builder(master, key) for key in master.loops}
    candidate_base = {key: base_builder(candidate, key) for key in candidate.loops}
    master_labels, candidate_labels = _assign_colors(master_base, candidate_base)
    rounds = 0
    stable = False
    for _ in range(8):
        master_signatures: Dict[LoopKey, Any] = {}
        candidate_signatures: Dict[LoopKey, Any] = {}
        for graph, labels, bases, destination in (
            (master, master_labels, master_base, master_signatures),
            (candidate, candidate_labels, candidate_base, candidate_signatures),
        ):
            for key, loop in graph.loops.items():
                face_loops = graph.faces[loop.face_key].loop_keys
                edge_loops = graph.edges[loop.edge_key].loop_keys
                vertex_loops = graph.vertices[loop.vertex_key].loop_keys
                destination[key] = (
                    bases[key],
                    tuple(sorted((labels[item] for item in face_loops if item != key), key=_stable_key)),
                    tuple(sorted((labels[item] for item in edge_loops if item != key), key=_stable_key)),
                    tuple(sorted((labels[item] for item in vertex_loops if item != key), key=_stable_key)),
                    tuple(sorted((labels[loop.next_key], labels[loop.prev_key]), key=_stable_key)),
                )
        new_master, new_candidate = _assign_colors(master_signatures, candidate_signatures)
        rounds += 1
        stable = new_master == master_labels and new_candidate == candidate_labels
        master_labels, candidate_labels = new_master, new_candidate
        if stable:
            break
    truncated = not stable
    master_counts: Dict[str, int] = {}
    candidate_counts: Dict[str, int] = {}
    for label in master_labels.values():
        master_counts[label] = master_counts.get(label, 0) + 1
    for label in candidate_labels.values():
        candidate_counts[label] = candidate_counts.get(label, 0) + 1
    pre_max = max(tuple(master_counts.values()) + tuple(candidate_counts.values()) or (0,))
    return master_labels, candidate_labels, rounds, stable, truncated, pre_max, 0


class _AmbiguousAssignment(Exception):
    pass


class _Runner:
    def __init__(self, master: Any, candidate: Any, options: VerifiedNearestOptions, seed: Any) -> None:
        self.master_input = master
        self.candidate_input = candidate
        self.options = options
        self.seed_input = seed
        self.started = time.perf_counter()
        self.master = None
        self.candidate = None
        self.seed = None
        self.domains: Dict[LoopKey, Tuple[LoopKey, ...]] = {}
        self.virtual_candidate_points: Dict[LoopKey, Point2] = {}
        self.master_points: Dict[LoopKey, Point2] = {}
        self.initial_domains: Tuple[Tuple[LoopKey, int], ...] = ()
        self.refinement_rounds = 0
        self.refinement_stable = False
        self.refinement_truncated = False
        self.refinement_pre_max = 0
        self.refinement_post_max = 0
        self.seed_supplied = seed is not None
        self.seed_validated = False
        self.seed_applied = False
        self.domain_count = 0
        self.domain_candidate_count = 0
        self._distance_cache: Dict[Tuple[LoopKey, LoopKey], float] = {}
        self.assignment_cap = 0
        self.distance_evaluations = 0
        self.distance_lookups = 0
        self.distance_cache_hits = 0
        self.distance_cache_misses = 0
        self.assignment_nodes = 0
        self.assignment_backtracks = 0
        self.complete_mappings = 0
        self.topology_checks = 0
        self.pruned_count = 0
        self.best_mapping: Optional[Dict[LoopKey, LoopKey]] = None
        self.best_verified = None
        self.best_cost = float("inf")
        self.ambiguous = False
        self.ambiguity_count = 0
        self.tie_count = 0
        self.max_seed_distance = 0.0
        self.mean_seed_distance = 0.0

    def _diagnostics(self, reason: str = "", *, topology_verified: bool = False, geometry_verified: bool = False) -> VerifiedNearestDiagnostics:
        elapsed = max(0, int(round((time.perf_counter() - self.started) * 1_000_000.0)))
        return VerifiedNearestDiagnostics(
            search_count=self.assignment_nodes,
            complete_mappings=self.complete_mappings,
            pruned_count=self.pruned_count,
            branch_budget=(self.assignment_cap or self.options.nearest_max_nodes),
            candidate_count=self.domain_candidate_count,
            topology_checks=self.topology_checks,
            initial_domain_sizes=self.initial_domains,
            yield_count=0,
            refinement_rounds=self.refinement_rounds,
            refinement_max_rounds=8,
            refinement_stable=self.refinement_stable,
            refinement_truncated=self.refinement_truncated,
            refinement_elapsed_us=0,
            refinement_pre_max_domain=self.refinement_pre_max,
            refinement_post_max_domain=self.refinement_post_max,
            domain_count=self.domain_count,
            domain_candidate_count=self.domain_candidate_count,
            distance_evaluations=self.distance_evaluations,
            assignment_nodes=self.assignment_nodes,
            assignment_backtracks=self.assignment_backtracks,
            ambiguous=self.ambiguous,
            seed_supplied=self.seed_supplied,
            seed_validated=self.seed_validated,
            seed_applied=self.seed_applied,
            topology_verified=topology_verified,
            geometry_verified=geometry_verified,
            max_seed_distance=self.max_seed_distance,
            mean_seed_distance=self.mean_seed_distance,
            ambiguity_count=self.ambiguity_count,
            tie_count=self.tie_count,
            assignment_cap=(self.assignment_cap or self.options.nearest_max_nodes),
            operations_used=self.assignment_nodes + self.distance_evaluations,
            distance_lookups=self.distance_lookups,
            distance_cache_hits=self.distance_cache_hits,
            distance_cache_misses=self.distance_cache_misses,
            fallback_reason_code=fallback_reason_code(reason),
            nearest_elapsed_us=elapsed,
            fallback_reason=reason,
        )

    def _fallback(self, reason: str) -> VerifiedNearestResult:
        diagnostics = self._diagnostics(reason)
        return VerifiedNearestResult(
            accepted=False,
            loop_mapping=(),
            reflected=False,
            reversed=False,
            cyclic_shift=0,
            score=float("inf"),
            residual=float("inf"),
            reason="fallback_required",
            transform=None,
            diagnostics=diagnostics,
            seed_transform=self.seed,
            fallback_reason=reason,
            nearest_diagnostics=diagnostics,
        )

    def _accepted(
        self,
        pairs: Tuple[Tuple[LoopKey, LoopKey], ...],
        *,
        reflected: bool,
        reversed_flag: bool,
        cyclic_shift: int,
        score: float,
        residual: float,
        transform: Any,
    ) -> VerifiedNearestResult:
        diagnostics = self._diagnostics("", topology_verified=True, geometry_verified=True)
        return VerifiedNearestResult(
            accepted=True,
            loop_mapping=pairs,
            reflected=reflected,
            reversed=reversed_flag,
            cyclic_shift=cyclic_shift,
            score=score,
            residual=residual,
            reason="accepted",
            transform=transform,
            diagnostics=diagnostics,
            seed_transform=self.seed,
            fallback_reason="",
            nearest_diagnostics=diagnostics,
        )

    def _prepare(self) -> Optional[VerifiedNearestResult]:
        try:
            self.master = _topology._validate_graph(_coerce_graph(self.master_input))
            self.candidate = _topology._validate_graph(_coerce_graph(self.candidate_input))
        except (Exception,):
            return self._fallback("invalid_graph")
        # A verified-nearest fast path needs explicit shape evidence.  Raw
        # coordinates alone are not a safe virtual alignment seed; leave that
        # case to the unchanged exact solver exactly once.
        if not self.seed_supplied:
            return self._fallback("missing_transform")
        if (
            len(self.master.loops) != len(self.candidate.loops)
            or len(self.master.faces) != len(self.candidate.faces)
            or len(self.master.edges) != len(self.candidate.edges)
            or len(self.master.vertices) != len(self.candidate.vertices)
            or len(self.master.boundaries) != len(self.candidate.boundaries)
        ):
            return self._fallback("topology_count_mismatch")
        degenerate = getattr(_topology, "_degenerate_uv_geometry")
        if degenerate(self.master) or degenerate(self.candidate):
            return self._fallback("degenerate_uv_geometry")
        if self.seed_supplied:
            try:
                self.seed = coerce_seed_transform(self.seed_input)
                self.seed_validated = self.seed is not None
            except (Exception,):
                return self._fallback("seed_transform_uncomposable")
        try:
            self.master_points = {
                key: _point(loop.uv, "master UV") for key, loop in self.master.loops.items()
            }
            raw_candidate_points = {
                key: _point(loop.uv, "candidate UV") for key, loop in self.candidate.loops.items()
            }
            if self.seed is None:
                self.virtual_candidate_points = dict(raw_candidate_points)
            else:
                self.virtual_candidate_points = {
                    key: _point(self.seed.apply(point), "seeded candidate UV")
                    for key, point in raw_candidate_points.items()
                }
                self.seed_applied = True
        except (Exception,):
            return self._fallback("seed_transform_uncomposable")
        try:
            (
                master_labels,
                candidate_labels,
                self.refinement_rounds,
                self.refinement_stable,
                self.refinement_truncated,
                self.refinement_pre_max,
                _unused,
            ) = _refine_labels(self.master, self.candidate)
            master_counts: Dict[str, int] = {}
            candidate_counts: Dict[str, int] = {}
            for label in master_labels.values():
                master_counts[label] = master_counts.get(label, 0) + 1
            for label in candidate_labels.values():
                candidate_counts[label] = candidate_counts.get(label, 0) + 1
            if master_counts != candidate_counts:
                return self._fallback("topology_mismatch")
            by_label: Dict[str, List[LoopKey]] = {}
            for key, label in master_labels.items():
                by_label.setdefault(label, []).append(key)
            base_builder = getattr(_topology, "_base_loop_signature")
            partial = getattr(_topology, "_partial_compatible")
            for candidate_key, label in candidate_labels.items():
                possible = []
                for master_key in sorted(by_label.get(label, ()), key=_loop_sort_key):
                    if base_builder(self.candidate, candidate_key) != base_builder(self.master, master_key):
                        continue
                    if partial(self.candidate, self.master, {}, candidate_key, master_key):
                        possible.append(master_key)
                if not possible:
                    return self._fallback("topology_mismatch")
                self.domains[candidate_key] = tuple(possible)
            self.initial_domains = tuple(
                (key, len(self.domains[key]))
                for key in sorted(self.domains, key=_loop_sort_key)
            )
            self.domain_count = len(self.domains)
            self.domain_candidate_count = sum(len(values) for values in self.domains.values())
            self.refinement_post_max = max((len(values) for values in self.domains.values()), default=0)
        except (Exception,):
            return self._fallback("topology_domain_derivation_failed")
        return None

    def _distance(self, candidate_key: LoopKey, master_key: LoopKey) -> float:
        cache_key = (candidate_key, master_key)
        self.distance_lookups += 1
        cached = self._distance_cache.get(cache_key)
        if cached is not None:
            self.distance_cache_hits += 1
            return cached
        self.distance_cache_misses += 1
        candidate_point = self.virtual_candidate_points[candidate_key]
        master_point = self.master_points[master_key]
        dx = candidate_point[0] - master_point[0]
        dy = candidate_point[1] - master_point[1]
        value = dx * dx + dy * dy
        self.distance_evaluations += 1
        if not math.isfinite(value):
            raise ValueError("nonfinite_nearest_distance")
        self._distance_cache[cache_key] = value
        return value

    def _tie_epsilon(self, values: Sequence[float]) -> float:
        magnitude = max([1.0] + [abs(value) for value in values])
        return max(float(self.options.nearest_tie_epsilon) * magnitude, 1.0e-15)

    def _ordered_options(self, candidate_key: LoopKey) -> List[Tuple[float, LoopKey]]:
        values = [(self._distance(candidate_key, master_key), master_key) for master_key in self.domains[candidate_key]]
        values.sort(key=lambda item: (item[0], _loop_sort_key(item[1])))
        if len(values) > 1 and abs(values[0][0] - values[1][0]) <= self._tie_epsilon((values[0][0], values[1][0])):
            # Equal local distances are diagnostic evidence, not a semantic
            # rejection.  The globally stable edge order below chooses one
            # bijection candidate, and the existing partial/full topology and
            # geometric verification remains the acceptance gate.
            self.ambiguous = True
            self.ambiguity_count += 1
            self.tie_count += 1
        return values

    def _mapping_tuple(self, mapping: Mapping[LoopKey, LoopKey]) -> Tuple[Tuple[LoopKey, LoopKey], ...]:
        return tuple(sorted(mapping.items(), key=lambda item: _loop_sort_key(item[0])))

    def _operation_cap(self) -> int:
        """Return a fixed linear cap for the optional greedy proof."""

        edge_count = sum(len(values) for values in self.domains.values())
        # Distance materialization and one-to-one proposal both consume the
        # bounded proof budget.  Keep a small structural margin for the
        # topology checks around each loop while retaining the hard 4096 cap.
        proportional = (2 * edge_count) + (4 * len(self.domains)) + 32
        return max(
            1,
            min(
                int(self.options.nearest_max_nodes),
                int(NEAREST_OPERATION_CAP),
                proportional,
            ),
        )

    def _search(self) -> Optional[str]:
        self.assignment_cap = self._operation_cap()
        if not self.domains:
            return "incomplete_mapping"
        edge_count = sum(len(values) for values in self.domains.values())
        if edge_count > self.assignment_cap:
            return "nearest_operation_cap"
        option_cache: Dict[LoopKey, List[Tuple[float, LoopKey]]] = {}
        try:
            for candidate_key in sorted(self.domains, key=_loop_sort_key):
                option_cache[candidate_key] = self._ordered_options(candidate_key)

            edges = [
                (distance, candidate_key, master_key)
                for candidate_key, values in option_cache.items()
                for distance, master_key in values
            ]
            edges.sort(
                key=lambda item: (
                    item[0],
                    _loop_sort_key(item[1]),
                    _loop_sort_key(item[2]),
                )
            )
            mapping: Dict[LoopKey, LoopKey] = {}
            used: set[LoopKey] = set()
            partial = getattr(_topology, "_partial_compatible")
            for _distance_value, candidate_key, master_key in edges:
                if (
                    self.assignment_nodes + self.distance_evaluations
                    >= self.assignment_cap
                ):
                    return "assignment_operation_cap"
                self.assignment_nodes += 1
                if candidate_key in mapping or master_key in used:
                    continue
                if not partial(
                    self.candidate,
                    self.master,
                    mapping,
                    candidate_key,
                    master_key,
                ):
                    self.pruned_count += 1
                    continue
                mapping[candidate_key] = master_key
                used.add(master_key)

            if len(mapping) != len(self.domains) or len(used) != len(self.domains):
                return "greedy_incomplete_mapping"
            self.complete_mappings = 1
            self.topology_checks += 1
            verifier = getattr(_topology, "_verify_full_mapping")
            verified = verifier(self.candidate, self.master, mapping)
            if verified is None:
                self.pruned_count += 1
                return "greedy_topology_verification_failed"
            self.best_mapping = dict(mapping)
            self.best_verified = verified
            self.best_cost = sum(
                self._distance(candidate_key, master_key)
                for candidate_key, master_key in mapping.items()
            )
        except _AmbiguousAssignment:
            return "ambiguous_assignment"
        except (Exception,):
            return "greedy_assignment_failed"
        if self.best_mapping is None:
            return "incomplete_mapping"
        return None

    def _fit_and_verify(self) -> VerifiedNearestResult:
        if self.best_mapping is None or self.best_verified is None:
            return self._fallback("incomplete_mapping")
        mapping = self.best_mapping
        pairs = self._mapping_tuple(mapping)
        distances = []
        for candidate_key, master_key in pairs:
            # Keep squared distances for deterministic ranking, but expose
            # ordinary Euclidean distance in the human-facing diagnostics.
            # The assignment phase already cached every compatible edge.  Use
            # the same cache here so the fit/diagnostic phase cannot turn a
            # bounded proof into a second distance pass.
            squared_distance = self._distance(candidate_key, master_key)
            distances.append(math.sqrt(max(0.0, squared_distance)))
        if distances:
            self.max_seed_distance = max(distances)
            self.mean_seed_distance = sum(distances) / len(distances)
        source_points = [_point(self.candidate.loops[key].uv, "candidate UV") for key, _ in pairs]
        target_points = [self.master_points[value] for _, value in pairs]
        fit_helper = getattr(_topology, "_fit_similarity")
        candidates = []
        for reflected in (False, True):
            fitted = fit_helper(
                source_points,
                target_points,
                reflected,
                self.options.match_scale,
            )
            if fitted is None:
                continue
            transform, residual = fitted
            if not math.isfinite(float(residual)) or not math.isfinite(float(transform.scale)) or transform.scale <= 0.0:
                continue
            face_orientation = self.best_verified[0]
            primary_face = sorted(face_orientation, key=_stable_key)[0]
            direction, shift = face_orientation[primary_face]
            reversed_flag = direction < 0
            effective_reflected = bool(reflected) ^ reversed_flag
            candidates.append(
                (
                    float(residual),
                    effective_reflected,
                    reversed_flag,
                    int(shift),
                    transform,
                )
            )
        if not candidates:
            return self._fallback("scale_or_geometry_fit_failed")
        candidates.sort(key=lambda item: (item[0], int(item[1]), item[3]))
        best = candidates[0]
        tolerance = float(self.options.tolerance)
        if self.ambiguous and self.seed is not None:
            # A symmetric tie may produce more than one topologically valid
            # bijection.  Accept the stable greedy choice only when its raw
            # UV refit also agrees with the virtual seed.  A seed that merely
            # happens to land on another valid symmetry is conservative
            # fallback evidence, not permission to guess.
            try:
                seed_fit_error = max(
                    math.dist(
                        self.seed.apply(source),
                        best[4].apply(source),
                    )
                    for source in source_points
                )
            except (Exception,):
                return self._fallback("seed_re_root_validation_failed")
            if not math.isfinite(seed_fit_error) or seed_fit_error > max(
                tolerance * 10.0,
                1.0e-9,
            ):
                return self._fallback("seed_re_root_validation_failed")
        if best[0] <= tolerance and (self.options.allow_flipping or not best[1]):
            pass
        elif not self.options.allow_flipping and any(
            item[1] and item[0] <= tolerance for item in candidates
        ):
            return self._fallback("reflection_not_allowed")
        elif best[0] > tolerance:
            if len({tuple(point) for point in source_points}) != len(source_points):
                return self._fallback("duplicate_nearest_conflict")
            return self._fallback("residual_above_tolerance")
        else:
            return self._fallback("reflection_not_allowed")
        target_span = getattr(_topology, "_uv_span")(target_points)
        score = best[0] / target_span
        if not math.isfinite(score):
            return self._fallback("scale_or_geometry_fit_failed")
        return self._accepted(
            pairs,
            reflected=best[1],
            reversed_flag=best[2],
            cyclic_shift=best[3],
            score=score,
            residual=best[0],
            transform=best[4],
        )

    def run(self) -> VerifiedNearestResult:
        prepared = self._prepare()
        if prepared is not None:
            return prepared
        assert self.master is not None and self.candidate is not None
        reason = self._search()
        if reason is not None:
            return self._fallback(reason)
        return self._fit_and_verify()


def find_verified_nearest(
    master: Any,
    candidate: Any,
    options: Any = None,
    seed_transform: Any = None,
    *,
    exact_options: Any = None,
    seed: Any = None,
    candidate_to_master: Any = None,
    allow_flipping: Optional[bool] = None,
    match_scale: Optional[bool] = None,
    tolerance: Optional[float] = None,
    max_search: Optional[int] = None,
    nearest_max_nodes: Optional[int] = None,
    nearest_tie_epsilon: Optional[float] = None,
    instrumentation: Any = None,
) -> VerifiedNearestResult:
    """Attempt a topology-verified nearest bijection.

    The function is pure with respect to the input graphs.  ``seed_transform``
    is applied only to copied coordinate tuples.  Absence of a seed is a
    non-applicable fast path; a supplied but invalid seed is a fallback.
    ``instrumentation`` is accepted as a compatibility hook; deterministic
    evidence is returned in ``result.diagnostics`` and no caller-owned object
    is mutated.
    """

    del instrumentation
    # Compatibility form for pure callers that pass the optional transform as
    # the third positional argument.
    if seed_transform is None and seed is None and options is not None:
        looks_like_seed = (
            isinstance(options, (tuple, list)) and len(options) == 5
        ) or all(hasattr(options, name) for name in ("angle", "scale"))
        if looks_like_seed:
            seed_transform = options
            options = None
    if seed is not None:
        if seed_transform is not None and seed != seed_transform:
            return VerifiedNearestResult(
                accepted=False,
                loop_mapping=(),
                reason="fallback_required",
                fallback_reason="multiple_seed_transforms",
                diagnostics=VerifiedNearestDiagnostics(
                    fallback_reason="multiple_seed_transforms",
                    fallback_reason_code=fallback_reason_code("multiple_seed_transforms"),
                ),
                nearest_diagnostics=VerifiedNearestDiagnostics(
                    fallback_reason="multiple_seed_transforms",
                    fallback_reason_code=fallback_reason_code("multiple_seed_transforms"),
                ),
            )
        seed_transform = seed
    if candidate_to_master is not None:
        if seed_transform is not None and candidate_to_master != seed_transform:
            # Two independently supplied seeds are not safely composable.
            return VerifiedNearestResult(
                accepted=False,
                loop_mapping=(),
                reason="fallback_required",
                fallback_reason="multiple_seed_transforms",
                diagnostics=VerifiedNearestDiagnostics(
                    fallback_reason="multiple_seed_transforms",
                    fallback_reason_code=fallback_reason_code("multiple_seed_transforms"),
                ),
                nearest_diagnostics=VerifiedNearestDiagnostics(
                    fallback_reason="multiple_seed_transforms",
                    fallback_reason_code=fallback_reason_code("multiple_seed_transforms"),
                ),
            )
        seed_transform = candidate_to_master
    try:
        resolved = _resolve_options(
            options,
            exact_options=exact_options,
            allow_flipping=allow_flipping,
            match_scale=match_scale,
            tolerance=tolerance,
            max_search=max_search,
            nearest_max_nodes=nearest_max_nodes,
            nearest_tie_epsilon=nearest_tie_epsilon,
        )
    except (Exception,):
        diagnostics = VerifiedNearestDiagnostics(
            fallback_reason="invalid_options",
            fallback_reason_code=fallback_reason_code("invalid_options"),
        )
        return VerifiedNearestResult(
            accepted=False,
            loop_mapping=(),
            reason="fallback_required",
            fallback_reason="invalid_options",
            diagnostics=diagnostics,
            nearest_diagnostics=diagnostics,
        )
    return _Runner(master, candidate, resolved, seed_transform).run()


def verified_nearest_correspondence(*args: Any, **kwargs: Any) -> VerifiedNearestResult:
    """Descriptive alias for :func:`find_verified_nearest`."""

    return find_verified_nearest(*args, **kwargs)


def find_verified_nearest_correspondence(*args: Any, **kwargs: Any) -> VerifiedNearestResult:
    return find_verified_nearest(*args, **kwargs)


def map_verified_nearest(*args: Any, **kwargs: Any) -> VerifiedNearestResult:
    return find_verified_nearest(*args, **kwargs)


def find_verified_nearest_mapping(*args: Any, **kwargs: Any) -> VerifiedNearestResult:
    return find_verified_nearest(*args, **kwargs)


def find_verified_nearest_or_fallback(
    master: Any,
    candidate: Any,
    transform: Any = None,
    *,
    allow_flipping: bool = False,
    match_scale: bool = True,
    tolerance: float = 1.0e-6,
    max_search: int = 100000,
) -> Any:
    """Run the fast proof and invoke the unchanged exact solver once on miss."""

    fast = find_verified_nearest(
        master,
        candidate,
        seed_transform=transform,
        allow_flipping=allow_flipping,
        match_scale=match_scale,
        tolerance=tolerance,
        max_search=max_search,
    )
    if bool(getattr(fast, "accepted", False)):
        return fast
    return _topology.find_correspondence(
        master,
        candidate,
        allow_flipping=allow_flipping,
        match_scale=match_scale,
        tolerance=tolerance,
        max_search=max_search,
    )


def find_verified_nearest_mapping(*args: Any, **kwargs: Any) -> VerifiedNearestResult:
    return find_verified_nearest(*args, **kwargs)


def verified_nearest_mapping(*args: Any, **kwargs: Any) -> VerifiedNearestResult:
    return find_verified_nearest(*args, **kwargs)


class VerifiedNearestMapper:
    """Small reusable object wrapper for callers that prefer an engine object."""

    def __init__(self, master: Any, candidate: Any, **kwargs: Any) -> None:
        self.master = master
        self.candidate = candidate
        self.kwargs = dict(kwargs)

    def run(self) -> VerifiedNearestResult:
        return find_verified_nearest(self.master, self.candidate, **self.kwargs)

    compute = run
    map = run


__all__ = [
    "CorrespondenceResult",
    "IslandGraph",
    "NEAREST_OPERATION_CAP",
    "SeedTransform",
    "SimilarityTransform",
    "SimilarityTransform2D",
    "SimilarityTransformSeed",
    "VerifiedNearestDiagnostics",
    "VerifiedNearestMapper",
    "VerifiedNearestOptions",
    "VerifiedNearestResult",
    "coerce_seed_transform",
    "find_verified_nearest",
    "find_verified_nearest_correspondence",
    "find_verified_nearest_mapping",
    "find_verified_nearest_or_fallback",
    "fallback_reason_code",
    "fallback_reason_name",
    "make_seed_transform",
    "map_verified_nearest",
    "normalize_seed_transform",
    "verified_nearest_mapping",
    "verified_nearest_correspondence",
]
