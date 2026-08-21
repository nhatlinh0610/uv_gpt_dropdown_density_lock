"""Pure, deterministic exact topology correspondence for UV loop graphs.

The module deliberately knows nothing about Blender.  A future BMesh adapter
can copy the required incidence into the immutable records below, run
``find_correspondence`` and then apply the returned loop mapping.  No BMesh
object or mutable runtime reference is retained here.
"""

from dataclasses import dataclass, field
from math import atan2, cos, hypot, isfinite, sin, sqrt
import time
from typing import Any, Dict, Hashable, Iterable, List, Mapping, Optional, Sequence, Tuple


LoopKey = Tuple[int, int]
NodeKey = Hashable
Point2 = Tuple[float, float]


def _stable_token(value: Any) -> Tuple[str, str]:
    """Return a comparable token for arbitrary adapter-owned record values."""

    return (type(value).__name__, repr(value))


def _stable_key(value: Any) -> Tuple[str, str]:
    return _stable_token(value)


def _stable_loop_key(value: LoopKey) -> Tuple[int, int]:
    return (int(value[0]), int(value[1]))


@dataclass(frozen=True)
class LoopRecord:
    """Immutable loop incidence and UV data.

    ``key`` is the stable ``(face_index, local_loop_index)`` identity used by
    the correspondence API.  UV loop identity is authoritative even when
    several loops refer to one mesh vertex.
    """

    key: LoopKey
    face_key: NodeKey
    edge_key: NodeKey
    vertex_key: NodeKey
    next_key: LoopKey
    prev_key: LoopKey
    uv: Point2
    boundary: bool = False
    seam: bool = False
    signature: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class FaceRecord:
    """A face and its ordered loop cycle."""

    key: NodeKey
    loop_keys: Tuple[LoopKey, ...]
    signature: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class EdgeRecord:
    """An edge's loops and radial face incidence."""

    key: NodeKey
    loop_keys: Tuple[LoopKey, ...]
    face_keys: Tuple[NodeKey, ...]
    boundary: bool = False
    non_manifold: bool = False
    signature: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class VertexRecord:
    """A mesh vertex's (possibly UV-split) loop incidence."""

    key: NodeKey
    loop_keys: Tuple[LoopKey, ...]
    boundary: bool = False
    signature: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class BoundaryComponentRecord:
    """An ordered boundary ring, including hole role/hierarchy metadata."""

    key: NodeKey
    loop_keys: Tuple[LoopKey, ...]
    role: str = "outer"
    parent_key: Optional[NodeKey] = None
    signature: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class IslandGraph:
    """Immutable graph records copied from one UV island."""

    faces: Tuple[FaceRecord, ...]
    edges: Tuple[EdgeRecord, ...]
    vertices: Tuple[VertexRecord, ...]
    loops: Tuple[LoopRecord, ...]
    boundaries: Tuple[BoundaryComponentRecord, ...] = ()


def make_graph(
    faces: Iterable[FaceRecord],
    edges: Iterable[EdgeRecord],
    vertices: Iterable[VertexRecord],
    loops: Iterable[LoopRecord],
    boundaries: Iterable[BoundaryComponentRecord] = (),
) -> IslandGraph:
    """Create an immutable graph from adapter or test record iterables."""

    return IslandGraph(
        faces=tuple(faces),
        edges=tuple(edges),
        vertices=tuple(vertices),
        loops=tuple(loops),
        boundaries=tuple(boundaries),
    )


@dataclass(frozen=True)
class SimilarityTransform2D:
    """A pure candidate-UV to master-UV similarity transform."""

    angle: float
    scale: float
    reflected: bool
    source_center: Point2
    target_center: Point2

    def apply(self, point: Point2) -> Point2:
        x = float(point[0]) - self.source_center[0]
        y = float(point[1]) - self.source_center[1]
        if self.reflected:
            x = -x
        c = cos(self.angle)
        s = sin(self.angle)
        return (
            self.target_center[0] + self.scale * (c * x - s * y),
            self.target_center[1] + self.scale * (s * x + c * y),
        )


@dataclass(frozen=True)
class CorrespondenceDiagnostics:
    """Bounded-search and validation evidence for one correspondence run."""

    search_count: int = 0
    complete_mappings: int = 0
    pruned_count: int = 0
    branch_budget: int = 0
    candidate_count: int = 0
    topology_checks: int = 0
    initial_domain_sizes: Tuple[Tuple[LoopKey, int], ...] = ()
    yield_count: int = 0
    # Exact loop refinement evidence is intentionally scalar and primitive so
    # it can cross the external-worker boundary without carrying graph data.
    refinement_rounds: int = 0
    refinement_max_rounds: int = 0
    refinement_stable: bool = False
    refinement_truncated: bool = False
    # Wall timing is observational and must not make sync/resumable logical
    # diagnostics compare unequal.
    refinement_elapsed_us: int = field(default=0, compare=False)
    refinement_pre_max_domain: int = 0
    refinement_post_max_domain: int = 0


@dataclass(frozen=True)
class CorrespondenceResult:
    """Structured exact-correspondence result.

    ``loop_mapping`` is an immutable tuple of ``(candidate_loop, master_loop)``
    pairs.  It is empty for every rejected result, so callers cannot
    accidentally apply a partial mapping.
    """

    accepted: bool
    loop_mapping: Tuple[Tuple[LoopKey, LoopKey], ...] = ()
    reflected: bool = False
    reversed: bool = False
    cyclic_shift: int = 0
    score: float = float("inf")
    residual: float = float("inf")
    reason: str = ""
    transform: Optional[SimilarityTransform2D] = None
    diagnostics: CorrespondenceDiagnostics = CorrespondenceDiagnostics()

    @property
    def mapping(self) -> Dict[LoopKey, LoopKey]:
        """Return a convenience copy for callers that need a mutable dict."""

        return dict(self.loop_mapping)


@dataclass(frozen=True)
class CorrespondenceStep:
    """One bounded advance of :class:`CorrespondenceSearch`.

    ``status`` is ``pending`` while more work remains, ``success`` or
    ``failure`` after a result is available, and ``cancelled`` after the
    caller explicitly discards the immutable search state.  The result is
    never populated for a pending/cancelled step, so a caller cannot apply a
    partial mapping.
    """

    status: str = "pending"
    result: Optional[CorrespondenceResult] = None
    diagnostics: CorrespondenceDiagnostics = CorrespondenceDiagnostics()
    operations: int = 0
    elapsed_ms: float = 0.0
    search_elapsed_ms: float = 0.0
    phase: str = ""


class _ValidationFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _SearchBudgetExceeded(Exception):
    pass


@dataclass
class _ValidatedGraph:
    graph: IslandGraph
    faces: Dict[NodeKey, FaceRecord]
    edges: Dict[NodeKey, EdgeRecord]
    vertices: Dict[NodeKey, VertexRecord]
    loops: Dict[LoopKey, LoopRecord]
    boundaries: Dict[NodeKey, BoundaryComponentRecord]
    loop_boundary: Dict[LoopKey, BoundaryComponentRecord]


def _unique_index(records: Iterable[Any], field: str, label: str) -> Dict[Any, Any]:
    result: Dict[Any, Any] = {}
    for record in records:
        key = getattr(record, field)
        if key in result:
            raise _ValidationFailure("invalid_record_duplicate_%s" % label)
        result[key] = record
    return result


def _validate_loop_key(key: Any) -> None:
    if (
        not isinstance(key, tuple)
        or len(key) != 2
        or not isinstance(key[0], int)
        or not isinstance(key[1], int)
        or key[0] < 0
        or key[1] < 0
    ):
        raise _ValidationFailure("invalid_record_loop_key")


def _validate_graph(graph: IslandGraph) -> _ValidatedGraph:
    faces = _unique_index(graph.faces, "key", "face")
    edges = _unique_index(graph.edges, "key", "edge")
    vertices = _unique_index(graph.vertices, "key", "vertex")
    loops = _unique_index(graph.loops, "key", "loop")
    boundaries = _unique_index(graph.boundaries, "key", "boundary")

    if not faces or not edges or not vertices or not loops:
        raise _ValidationFailure("invalid_record_empty_graph")

    for key, loop in loops.items():
        _validate_loop_key(key)
        if loop.face_key not in faces:
            raise _ValidationFailure("invalid_record_loop_face_incidence")
        if loop.edge_key not in edges:
            raise _ValidationFailure("invalid_record_loop_edge_incidence")
        if loop.vertex_key not in vertices:
            raise _ValidationFailure("invalid_record_loop_vertex_incidence")
        if loop.next_key not in loops or loop.prev_key not in loops:
            raise _ValidationFailure("invalid_record_loop_next_prev")
        if (
            not isinstance(loop.uv, tuple)
            or len(loop.uv) != 2
            or not isfinite(float(loop.uv[0]))
            or not isfinite(float(loop.uv[1]))
        ):
            raise _ValidationFailure("invalid_record_uv")

    for face in faces.values():
        if len(face.loop_keys) < 3 or len(set(face.loop_keys)) != len(face.loop_keys):
            raise _ValidationFailure("invalid_record_face_cycle")
        if any(key not in loops for key in face.loop_keys):
            raise _ValidationFailure("invalid_record_face_loop_incidence")
        if any(loops[key].face_key != face.key for key in face.loop_keys):
            raise _ValidationFailure("invalid_record_face_loop_incidence")
        expected = tuple(face.loop_keys)
        if tuple(loops[key].next_key for key in expected) != tuple(
            expected[(index + 1) % len(expected)] for index in range(len(expected))
        ):
            raise _ValidationFailure("invalid_record_ordered_face_cycle")
        if tuple(loops[key].prev_key for key in expected) != tuple(
            expected[(index - 1) % len(expected)] for index in range(len(expected))
        ):
            raise _ValidationFailure("invalid_record_ordered_face_cycle")

    for loop in loops.values():
        if loops[loop.next_key].prev_key != loop.key or loops[loop.prev_key].next_key != loop.key:
            raise _ValidationFailure("invalid_record_next_prev_relation")

    derived_edge_loops: Dict[NodeKey, List[LoopKey]] = {key: [] for key in edges}
    derived_vertex_loops: Dict[NodeKey, List[LoopKey]] = {key: [] for key in vertices}
    for loop in loops.values():
        derived_edge_loops[loop.edge_key].append(loop.key)
        derived_vertex_loops[loop.vertex_key].append(loop.key)

    for edge in edges.values():
        actual_loops = tuple(sorted(derived_edge_loops[edge.key], key=_stable_loop_key))
        listed_loops = tuple(sorted(edge.loop_keys, key=_stable_loop_key))
        if not actual_loops or actual_loops != listed_loops:
            raise _ValidationFailure("invalid_record_edge_loop_incidence")
        actual_faces = tuple(sorted({loops[key].face_key for key in actual_loops}, key=_stable_key))
        listed_faces = tuple(sorted(set(edge.face_keys), key=_stable_key))
        if not listed_faces or actual_faces != listed_faces:
            raise _ValidationFailure("invalid_record_edge_face_incidence")
        if edge.non_manifold or len(actual_faces) > 2:
            raise _ValidationFailure("non_manifold_topology")
        if edge.boundary != (len(actual_faces) == 1):
            raise _ValidationFailure("invalid_record_edge_boundary_state")

    for vertex in vertices.values():
        actual_loops = tuple(sorted(derived_vertex_loops[vertex.key], key=_stable_loop_key))
        listed_loops = tuple(sorted(vertex.loop_keys, key=_stable_loop_key))
        if not actual_loops or actual_loops != listed_loops:
            raise _ValidationFailure("invalid_record_vertex_loop_incidence")
        actual_boundary = any(edges[loops[key].edge_key].boundary for key in actual_loops)
        if vertex.boundary != actual_boundary:
            raise _ValidationFailure("invalid_record_vertex_boundary_state")

    for loop in loops.values():
        if loop.boundary != edges[loop.edge_key].boundary:
            raise _ValidationFailure("invalid_record_loop_boundary_state")

    loop_boundary: Dict[LoopKey, BoundaryComponentRecord] = {}
    for component in boundaries.values():
        if not component.role or len(component.loop_keys) < 3:
            raise _ValidationFailure("invalid_record_boundary_component")
        if len(set(component.loop_keys)) != len(component.loop_keys):
            raise _ValidationFailure("invalid_record_boundary_component")
        for key in component.loop_keys:
            if key not in loops or not loops[key].boundary:
                raise _ValidationFailure("invalid_record_boundary_loop_incidence")
            if key in loop_boundary:
                raise _ValidationFailure("invalid_record_boundary_overlap")
            loop_boundary[key] = component
        if component.parent_key is not None and component.parent_key not in boundaries:
            raise _ValidationFailure("invalid_record_boundary_parent")
        if component.parent_key == component.key:
            raise _ValidationFailure("invalid_record_boundary_parent")

    all_boundary_loops = {loop.key for loop in loops.values() if loop.boundary}
    if all_boundary_loops != set(loop_boundary):
        raise _ValidationFailure("invalid_record_boundary_coverage")

    for component in boundaries.values():
        seen = set()
        current: Optional[NodeKey] = component.parent_key
        while current is not None:
            if current in seen:
                raise _ValidationFailure("invalid_record_boundary_hierarchy")
            seen.add(current)
            current = boundaries[current].parent_key

    return _ValidatedGraph(
        graph=graph,
        faces=faces,
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=boundaries,
        loop_boundary=loop_boundary,
    )


def _boundary_semantic(
    validated: _ValidatedGraph, key: LoopKey
) -> Optional[Tuple[Any, ...]]:
    component = validated.loop_boundary.get(key)
    if component is None:
        return None
    parent_role: Optional[str] = None
    if component.parent_key is not None:
        parent_role = validated.boundaries[component.parent_key].role
    return (
        component.role,
        parent_role,
        len(component.loop_keys),
        _stable_token(component.signature),
    )


def _base_loop_signature(validated: _ValidatedGraph, key: LoopKey) -> Tuple[Any, ...]:
    loop = validated.loops[key]
    face = validated.faces[loop.face_key]
    edge = validated.edges[loop.edge_key]
    vertex = validated.vertices[loop.vertex_key]
    return (
        bool(loop.boundary),
        bool(loop.seam),
        (len(face.loop_keys), _stable_token(face.signature)),
        (
            bool(edge.boundary),
            len(edge.loop_keys),
            len(edge.face_keys),
            bool(edge.non_manifold),
            _stable_token(edge.signature),
        ),
        (
            bool(vertex.boundary),
            len(vertex.loop_keys),
            _stable_token(vertex.signature),
        ),
        _boundary_semantic(validated, key),
        _stable_token(loop.signature),
    )


def _sorted_labels(values: Iterable[Any]) -> Tuple[Any, ...]:
    return tuple(sorted(values, key=_stable_key))


def _label_histogram(labels: Mapping[LoopKey, Any]) -> Tuple[Tuple[Any, int], ...]:
    """Return a deterministic class histogram for one refined partition."""

    counts: Dict[Any, int] = {}
    for label in labels.values():
        counts[label] = counts.get(label, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: _stable_key(item[0])))


def _same_partition(
    previous: Mapping[LoopKey, Any], current: Mapping[LoopKey, Any]
) -> bool:
    """Compare partitions without depending on color-token names or IDs."""

    if set(previous) != set(current):
        return False
    previous_to_current: Dict[Any, Any] = {}
    current_to_previous: Dict[Any, Any] = {}
    for key in sorted(previous, key=_stable_loop_key):
        left = previous[key]
        right = current[key]
        if left in previous_to_current and previous_to_current[left] != right:
            return False
        if right in current_to_previous and current_to_previous[right] != left:
            return False
        previous_to_current[left] = right
        current_to_previous[right] = left
    return True


def _refine_labels(
    master: _ValidatedGraph, candidate: _ValidatedGraph
) -> Tuple[Dict[LoopKey, str], Dict[LoopKey, str]]:
    """Perform joint Weisfeiler-style refinement, ignoring next/prev direction."""

    master_base = {key: _base_loop_signature(master, key) for key in master.loops}
    candidate_base = {key: _base_loop_signature(candidate, key) for key in candidate.loops}

    def assign_colors(
        master_signatures: Mapping[LoopKey, Any], candidate_signatures: Mapping[LoopKey, Any]
    ) -> Tuple[Dict[LoopKey, str], Dict[LoopKey, str]]:
        signatures = list(master_signatures.values()) + list(candidate_signatures.values())
        unique = sorted(set(signatures), key=_stable_key)
        colors = {signature: "c%06d" % index for index, signature in enumerate(unique)}
        return (
            {key: colors[value] for key, value in master_signatures.items()},
            {key: colors[value] for key, value in candidate_signatures.items()},
        )

    master_labels, candidate_labels = assign_colors(master_base, candidate_base)
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
                    _sorted_labels(labels[item] for item in face_loops if item != key),
                    _sorted_labels(labels[item] for item in edge_loops if item != key),
                    _sorted_labels(labels[item] for item in vertex_loops if item != key),
                    _sorted_labels((labels[loop.next_key], labels[loop.prev_key])),
                )
        new_master, new_candidate = assign_colors(master_signatures, candidate_signatures)
        if new_master == master_labels and new_candidate == candidate_labels:
            break
        master_labels, candidate_labels = new_master, new_candidate
    return master_labels, candidate_labels


def _cycle_match(
    mapped_cycle: Sequence[LoopKey], master_cycle: Sequence[LoopKey]
) -> Optional[Tuple[int, int]]:
    if len(mapped_cycle) != len(master_cycle) or not master_cycle:
        return None
    mapped = tuple(mapped_cycle)
    master = tuple(master_cycle)
    length = len(master)
    for direction in (1, -1):
        for shift in range(length):
            expected = tuple(master[(shift + direction * index) % length] for index in range(length))
            if mapped == expected:
                return direction, shift
    return None


def _partial_face_options(
    candidate: _ValidatedGraph,
    master: _ValidatedGraph,
    mapping: Mapping[LoopKey, LoopKey],
    candidate_face_key: NodeKey,
    master_face_key: NodeKey,
) -> Tuple[Tuple[int, int], ...]:
    candidate_cycle = candidate.faces[candidate_face_key].loop_keys
    master_cycle = master.faces[master_face_key].loop_keys
    if len(candidate_cycle) != len(master_cycle):
        return ()
    positions: Dict[LoopKey, int] = {key: index for index, key in enumerate(master_cycle)}
    pairs = [
        (index, positions[mapping[key]])
        for index, key in enumerate(candidate_cycle)
        if key in mapping and mapping[key] in positions
    ]
    options: List[Tuple[int, int]] = []
    for direction in (1, -1):
        shift: Optional[int] = None
        valid = True
        for candidate_index, master_index in pairs:
            expected_shift = (master_index - direction * candidate_index) % len(master_cycle)
            if shift is None:
                shift = expected_shift
            elif shift != expected_shift:
                valid = False
                break
        if valid:
            options.append((direction, 0 if shift is None else shift))
    return tuple(options)


def _partial_compatible(
    candidate: _ValidatedGraph,
    master: _ValidatedGraph,
    mapping: Mapping[LoopKey, LoopKey],
    candidate_key: LoopKey,
    master_key: LoopKey,
) -> bool:
    candidate_loop = candidate.loops[candidate_key]
    master_loop = master.loops[master_key]
    candidate_face = candidate_loop.face_key
    master_face = master_loop.face_key

    # Every incidence type is mapped as a typed bijection while the search is
    # still partial.  This prevents a loop from silently changing radial or
    # shared-vertex context.
    related_pairs = (
        (candidate_loop.face_key, master_loop.face_key, "face_key"),
        (candidate_loop.edge_key, master_loop.edge_key, "edge_key"),
        (candidate_loop.vertex_key, master_loop.vertex_key, "vertex_key"),
    )
    for candidate_node, master_node, attribute in related_pairs:
        # Node maps are derived from already assigned loops.
        for other_candidate, other_master in mapping.items():
            other_loop = candidate.loops[other_candidate]
            other_master_loop = master.loops[other_master]
            if getattr(other_loop, attribute) == candidate_node:
                if getattr(other_master_loop, attribute) != master_node:
                    return False
            if getattr(other_master_loop, attribute) == master_node:
                if getattr(other_loop, attribute) != candidate_node:
                    return False

    candidate_component = candidate.loop_boundary.get(candidate_key)
    master_component = master.loop_boundary.get(master_key)
    if (candidate_component is None) != (master_component is None):
        return False
    if candidate_component is not None and master_component is not None:
        if _boundary_semantic(candidate, candidate_key) != _boundary_semantic(master, master_key):
            return False
        for other_candidate, other_master in mapping.items():
            if candidate.loop_boundary.get(other_candidate) == candidate_component:
                if master.loop_boundary.get(other_master) != master_component:
                    return False
            if master.loop_boundary.get(other_master) == master_component:
                if candidate.loop_boundary.get(other_candidate) != candidate_component:
                    return False

    if not _partial_face_options(candidate, master, mapping, candidate_face, master_face):
        return False

    # A mapped next/prev neighbor must remain a neighbor.  Direction is
    # intentionally left to the ordered-cycle verifier so both orientation
    # hypotheses remain available during search.
    for neighbor in (candidate_loop.next_key, candidate_loop.prev_key):
        if neighbor in mapping:
            mapped_neighbor = mapping[neighbor]
            if mapped_neighbor not in (master_loop.next_key, master_loop.prev_key):
                return False
    return True


def _derive_node_map(
    candidate: _ValidatedGraph,
    master: _ValidatedGraph,
    mapping: Mapping[LoopKey, LoopKey],
    candidate_keys: Iterable[NodeKey],
    getter: str,
) -> Optional[Dict[NodeKey, NodeKey]]:
    result: Dict[NodeKey, NodeKey] = {}
    reverse: Dict[NodeKey, NodeKey] = {}
    for candidate_loop_key in candidate_keys:
        candidate_node = getattr(candidate.loops[candidate_loop_key], getter)
        master_node = getattr(master.loops[mapping[candidate_loop_key]], getter)
        if candidate_node in result and result[candidate_node] != master_node:
            return None
        if master_node in reverse and reverse[master_node] != candidate_node:
            return None
        result[candidate_node] = master_node
        reverse[master_node] = candidate_node
    return result


def _verify_full_mapping(
    candidate: _ValidatedGraph,
    master: _ValidatedGraph,
    mapping: Mapping[LoopKey, LoopKey],
) -> Optional[Tuple[Dict[NodeKey, Tuple[int, int]], Dict[NodeKey, NodeKey]]]:
    if len(mapping) != len(candidate.loops) or set(mapping) != set(candidate.loops):
        return None
    if len(set(mapping.values())) != len(master.loops) or set(mapping.values()) != set(master.loops):
        return None

    face_map = _derive_node_map(candidate, master, mapping, candidate.loops, "face_key")
    edge_map = _derive_node_map(candidate, master, mapping, candidate.loops, "edge_key")
    vertex_map = _derive_node_map(candidate, master, mapping, candidate.loops, "vertex_key")
    if face_map is None or edge_map is None or vertex_map is None:
        return None
    if set(face_map.values()) != set(master.faces):
        return None
    if set(edge_map.values()) != set(master.edges):
        return None
    if set(vertex_map.values()) != set(master.vertices):
        return None

    face_orientation: Dict[NodeKey, Tuple[int, int]] = {}
    for candidate_face_key, candidate_face in candidate.faces.items():
        master_face_key = face_map.get(candidate_face_key)
        if master_face_key is None:
            return None
        mapped_cycle = tuple(mapping[key] for key in candidate_face.loop_keys)
        master_face = master.faces[master_face_key]
        cycle_info = _cycle_match(mapped_cycle, master_face.loop_keys)
        if cycle_info is None:
            return None
        if candidate_face.signature != master_face.signature:
            return None
        face_orientation[candidate_face_key] = cycle_info

    for candidate_edge_key, candidate_edge in candidate.edges.items():
        master_edge_key = edge_map.get(candidate_edge_key)
        if master_edge_key is None:
            return None
        master_edge = master.edges[master_edge_key]
        mapped_loops = {mapping[key] for key in candidate_edge.loop_keys}
        if mapped_loops != set(master_edge.loop_keys):
            return None
        mapped_faces = {face_map[key] for key in candidate_edge.face_keys}
        if mapped_faces != set(master_edge.face_keys):
            return None
        if (
            candidate_edge.boundary != master_edge.boundary
            or candidate_edge.non_manifold != master_edge.non_manifold
            or candidate_edge.signature != master_edge.signature
        ):
            return None

    for candidate_vertex_key, candidate_vertex in candidate.vertices.items():
        master_vertex_key = vertex_map.get(candidate_vertex_key)
        if master_vertex_key is None:
            return None
        master_vertex = master.vertices[master_vertex_key]
        if {mapping[key] for key in candidate_vertex.loop_keys} != set(master_vertex.loop_keys):
            return None
        if candidate_vertex.boundary != master_vertex.boundary:
            return None
        if candidate_vertex.signature != master_vertex.signature:
            return None

    component_map: Dict[NodeKey, NodeKey] = {}
    reverse_component: Dict[NodeKey, NodeKey] = {}
    for candidate_component_key, component in candidate.boundaries.items():
        mapped_loops = tuple(mapping[key] for key in component.loop_keys)
        if not mapped_loops:
            return None
        master_component_key = master.loop_boundary.get(mapped_loops[0])
        if master_component_key is None:
            return None
        master_component_key = master_component_key.key
        master_component = master.boundaries[master_component_key]
        if _cycle_match(mapped_loops, master_component.loop_keys) is None:
            return None
        if (
            component.role != master_component.role
            or component.signature != master_component.signature
            or len(component.loop_keys) != len(master_component.loop_keys)
        ):
            return None
        if component.parent_key is None:
            if master_component.parent_key is not None:
                return None
        elif component.parent_key in component_map:
            if component_map[component.parent_key] != master_component.parent_key:
                return None
        component_map[candidate_component_key] = master_component_key
        if master_component_key in reverse_component:
            return None
        reverse_component[master_component_key] = candidate_component_key

    if set(component_map.values()) != set(master.boundaries):
        return None
    for candidate_component_key, component in candidate.boundaries.items():
        if component.parent_key is not None:
            if component.parent_key not in component_map:
                return None
            mapped_parent = component_map[component.parent_key]
            if master.boundaries[component_map[candidate_component_key]].parent_key != mapped_parent:
                return None

    for candidate_key, master_key in mapping.items():
        candidate_loop = candidate.loops[candidate_key]
        master_loop = master.loops[master_key]
        if (
            candidate_loop.boundary != master_loop.boundary
            or candidate_loop.seam != master_loop.seam
            or candidate_loop.signature != master_loop.signature
        ):
            return None

    return face_orientation, component_map


def _fit_similarity(
    source_points: Sequence[Point2],
    target_points: Sequence[Point2],
    reflected: bool,
    match_scale: bool,
) -> Optional[Tuple[SimilarityTransform2D, float]]:
    if len(source_points) != len(target_points) or not source_points:
        return None
    source_center = (
        sum(float(point[0]) for point in source_points) / len(source_points),
        sum(float(point[1]) for point in source_points) / len(source_points),
    )
    target_center = (
        sum(float(point[0]) for point in target_points) / len(target_points),
        sum(float(point[1]) for point in target_points) / len(target_points),
    )
    centered_source: List[Point2] = []
    centered_target: List[Point2] = []
    denominator = 0.0
    dot = 0.0
    cross = 0.0
    for source, target in zip(source_points, target_points):
        x = float(source[0]) - source_center[0]
        y = float(source[1]) - source_center[1]
        if reflected:
            x = -x
        dx = float(target[0]) - target_center[0]
        dy = float(target[1]) - target_center[1]
        centered_source.append((x, y))
        centered_target.append((dx, dy))
        denominator += x * x + y * y
        dot += x * dx + y * dy
        cross += x * dy - y * dx
    if denominator <= 1.0e-24:
        return None
    scale = hypot(dot, cross) / denominator if match_scale else 1.0
    angle = atan2(cross, dot)
    transform = SimilarityTransform2D(
        angle=angle,
        scale=scale,
        reflected=reflected,
        source_center=source_center,
        target_center=target_center,
    )
    squared_error = 0.0
    for source, target in zip(source_points, target_points):
        fitted = transform.apply(source)
        squared_error += (fitted[0] - target[0]) ** 2 + (fitted[1] - target[1]) ** 2
    residual = sqrt(squared_error / len(source_points))
    return transform, residual


def _uv_span(points: Sequence[Point2]) -> float:
    if not points:
        return 1.0
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    return max(hypot(max_x - min_x, max_y - min_y), 1.0e-12)


def _degenerate_uv_geometry(validated: _ValidatedGraph) -> bool:
    """Reject zero-rank UV data before it can create an arbitrary fit."""

    points = [loop.uv for loop in validated.loops.values()]
    unique = []
    seen = set()
    for point in points:
        if point not in seen:
            seen.add(point)
            unique.append(point)
    if len(unique) < 2:
        return True
    anchor = unique[0]
    direction: Optional[Point2] = None
    for point in unique[1:]:
        vector = (point[0] - anchor[0], point[1] - anchor[1])
        if hypot(vector[0], vector[1]) > 1.0e-12:
            direction = vector
            break
    if direction is None:
        return True
    scale = max(_uv_span(points) ** 2, 1.0e-24)
    return all(
        abs(direction[0] * (point[1] - anchor[1]) - direction[1] * (point[0] - anchor[0]))
        <= 1.0e-12 * scale
        for point in unique
    )


def _rejected(
    reason: str,
    diagnostics: CorrespondenceDiagnostics,
) -> CorrespondenceResult:
    return CorrespondenceResult(accepted=False, reason=reason, diagnostics=diagnostics)


class CorrespondenceSearch:
    """Deterministic resumable exact correspondence engine.

    The synchronous public API and Align Similar Pro both use this state
    machine.  Each advance performs one bounded preparation/search operation;
    the explicit DFS frames make pausing independent of Python recursion and
    avoid retaining a partially applicable mapping in a result.
    """

    _MAX_REFINEMENT_ROUNDS = 8

    def __init__(
        self,
        master: IslandGraph,
        candidate: IslandGraph,
        *,
        allow_flipping: bool = False,
        match_scale: bool = True,
        tolerance: float = 1.0e-6,
        max_search: int = 100000,
        cooperative_yield_every: int = 0,
    ) -> None:
        try:
            cooperative_yield_every = int(cooperative_yield_every)
        except (TypeError, ValueError):
            cooperative_yield_every = 0
        self.master = master
        self.candidate = candidate
        self.allow_flipping = bool(allow_flipping)
        self.match_scale = bool(match_scale)
        self.tolerance = tolerance
        self.max_search = max_search
        self.cooperative_yield_every = max(0, cooperative_yield_every)
        self._phase = "initial"
        self._result: Optional[CorrespondenceResult] = None
        self._cancelled = False
        self._diagnostics = CorrespondenceDiagnostics(branch_budget=max_search)
        self._master_graph: Optional[_ValidatedGraph] = None
        self._candidate_graph: Optional[_ValidatedGraph] = None
        self._master_labels: Dict[LoopKey, str] = {}
        self._candidate_labels: Dict[LoopKey, str] = {}
        self._master_base: Dict[LoopKey, Any] = {}
        self._candidate_base: Dict[LoopKey, Any] = {}
        self._refinement_round = 0
        self._refinement_max_rounds = 0
        self._refinement_started_at: Optional[float] = None
        self._refinement_elapsed_us = 0
        self._refinement_stable = False
        self._refinement_truncated = False
        self._refinement_pre_max_domain = 0
        self._refinement_post_max_domain = 0
        self._master_by_label: Dict[str, List[LoopKey]] = {}
        self._candidate_by_label: Dict[str, List[LoopKey]] = {}
        self._domain_keys: Tuple[LoopKey, ...] = ()
        self._domain_index = 0
        self._domains: Dict[LoopKey, Tuple[LoopKey, ...]] = {}
        self._initial_domains: Tuple[Tuple[LoopKey, int], ...] = ()
        self._search_order: Tuple[LoopKey, ...] = ()
        self._mapping: Dict[LoopKey, LoopKey] = {}
        self._used_master: set = set()
        self._frames: List[Dict[str, Any]] = []
        self._state = {
            "search_count": 0,
            "complete_mappings": 0,
            "pruned_count": 0,
            "topology_checks": 0,
            "yield_count": 0,
        }
        self._master_points_by_key: Dict[LoopKey, Point2] = {}
        self._candidate_points_by_key: Dict[LoopKey, Point2] = {}
        self._best_accepted: Optional[CorrespondenceResult] = None
        self._best_fit: Optional[CorrespondenceResult] = None
        self._best_disallowed_reflection: Optional[CorrespondenceResult] = None
        self._operations_total = 0
        self._search_operations_total = 0
        self._last_step = CorrespondenceStep(diagnostics=self._diagnostics)

    @property
    def state(self) -> str:
        return self._phase

    @property
    def done(self) -> bool:
        return self._phase in {"done", "cancelled"}

    @property
    def pending(self) -> bool:
        return not self.done

    @property
    def result(self) -> Optional[CorrespondenceResult]:
        return self._result

    @property
    def diagnostics(self) -> CorrespondenceDiagnostics:
        return self._diagnostics

    @property
    def operations_total(self) -> int:
        return self._operations_total

    @property
    def search_operations_total(self) -> int:
        return self._search_operations_total

    def _assign_colors(
        self,
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

    def _current_diagnostics(self) -> CorrespondenceDiagnostics:
        return CorrespondenceDiagnostics(
            search_count=self._state["search_count"],
            complete_mappings=self._state["complete_mappings"],
            pruned_count=self._state["pruned_count"],
            branch_budget=self.max_search,
            candidate_count=sum(len(values) for values in self._domains.values()),
            topology_checks=self._state["topology_checks"],
            initial_domain_sizes=self._initial_domains,
            yield_count=self._state["yield_count"],
            refinement_rounds=self._refinement_round,
            refinement_max_rounds=self._refinement_max_rounds,
            refinement_stable=self._refinement_stable,
            refinement_truncated=self._refinement_truncated,
            refinement_elapsed_us=self._refinement_elapsed_us,
            refinement_pre_max_domain=self._refinement_pre_max_domain,
            refinement_post_max_domain=self._refinement_post_max_domain,
        )

    def _update_refinement_elapsed(self) -> None:
        if self._refinement_started_at is not None:
            self._refinement_elapsed_us = max(
                0,
                int(
                    round(
                        (time.perf_counter() - self._refinement_started_at)
                        * 1_000_000.0
                    )
                ),
            )

    def _finish(self, result: CorrespondenceResult) -> None:
        self._result = result
        self._diagnostics = result.diagnostics
        self._phase = "done"

    def _finish_rejected(self, reason: str) -> None:
        self._finish(_rejected(reason, self._current_diagnostics()))

    def cancel(self) -> None:
        """Discard the resumable state without producing an applicable result."""

        if self.done:
            return
        self._mapping.clear()
        self._used_master.clear()
        self._frames.clear()
        self._cancelled = True
        self._result = None
        self._phase = "cancelled"

    def _advance_one(self) -> None:
        if self._phase == "initial":
            diagnostics = CorrespondenceDiagnostics(branch_budget=self.max_search)
            if self.max_search <= 0:
                self._finish(_rejected("search_budget_exceeded", diagnostics))
                return
            if not isfinite(float(self.tolerance)) or float(self.tolerance) < 0.0:
                self._finish(_rejected("invalid_tolerance", diagnostics))
                return
            self._phase = "validate_master"
            return

        if self._phase == "validate_master":
            try:
                self._master_graph = _validate_graph(self.master)
            except _ValidationFailure as error:
                self._finish_rejected(error.reason)
                return
            self._phase = "validate_candidate"
            return

        if self._phase == "validate_candidate":
            try:
                self._candidate_graph = _validate_graph(self.candidate)
            except _ValidationFailure as error:
                self._finish_rejected(error.reason)
                return
            self._phase = "check_counts"
            return

        if self._phase == "check_counts":
            assert self._master_graph is not None
            assert self._candidate_graph is not None
            if len(self._master_graph.loops) != len(self._candidate_graph.loops):
                self._finish_rejected("topology_count_mismatch")
                return
            if (
                len(self._master_graph.faces) != len(self._candidate_graph.faces)
                or len(self._master_graph.edges) != len(self._candidate_graph.edges)
                or len(self._master_graph.vertices) != len(self._candidate_graph.vertices)
                or len(self._master_graph.boundaries) != len(self._candidate_graph.boundaries)
            ):
                self._finish_rejected("topology_count_mismatch")
                return
            self._phase = "check_geometry"
            return

        if self._phase == "check_geometry":
            assert self._master_graph is not None
            assert self._candidate_graph is not None
            if _degenerate_uv_geometry(self._master_graph) or _degenerate_uv_geometry(
                self._candidate_graph
            ):
                self._finish_rejected("degenerate_uv_geometry")
                return
            self._phase = "refine_init"
            return

        if self._phase == "refine_init":
            assert self._master_graph is not None
            assert self._candidate_graph is not None
            self._master_base = {
                key: _base_loop_signature(self._master_graph, key)
                for key in self._master_graph.loops
            }
            self._candidate_base = {
                key: _base_loop_signature(self._candidate_graph, key)
                for key in self._candidate_graph.loops
            }
            self._master_labels, self._candidate_labels = self._assign_colors(
                self._master_base,
                self._candidate_base,
            )
            self._refinement_round = 0
            self._refinement_max_rounds = self._MAX_REFINEMENT_ROUNDS
            self._refinement_started_at = time.perf_counter()
            self._refinement_elapsed_us = 0
            self._refinement_stable = False
            self._refinement_truncated = False
            self._refinement_pre_max_domain = 0
            self._refinement_post_max_domain = 0
            self._phase = "refine_round"
            return

        if self._phase == "refine_round":
            assert self._master_graph is not None
            assert self._candidate_graph is not None
            master_signatures: Dict[LoopKey, Any] = {}
            candidate_signatures: Dict[LoopKey, Any] = {}
            for graph, labels, bases, destination in (
                (
                    self._master_graph,
                    self._master_labels,
                    self._master_base,
                    master_signatures,
                ),
                (
                    self._candidate_graph,
                    self._candidate_labels,
                    self._candidate_base,
                    candidate_signatures,
                ),
            ):
                for key, loop in graph.loops.items():
                    face_loops = graph.faces[loop.face_key].loop_keys
                    edge_loops = graph.edges[loop.edge_key].loop_keys
                    vertex_loops = graph.vertices[loop.vertex_key].loop_keys
                    destination[key] = (
                        bases[key],
                        _sorted_labels(labels[item] for item in face_loops if item != key),
                        _sorted_labels(labels[item] for item in edge_loops if item != key),
                        _sorted_labels(labels[item] for item in vertex_loops if item != key),
                        _sorted_labels((labels[loop.next_key], labels[loop.prev_key])),
                    )
            new_master, new_candidate = self._assign_colors(
                master_signatures,
                candidate_signatures,
            )
            self._refinement_round += 1
            stable = (
                new_master == self._master_labels
                and new_candidate == self._candidate_labels
            )
            self._master_labels, self._candidate_labels = new_master, new_candidate
            self._update_refinement_elapsed()
            if stable or self._refinement_round >= self._MAX_REFINEMENT_ROUNDS:
                self._refinement_stable = stable
                self._refinement_truncated = not stable
                self._phase = "build_label_buckets"
            return

        if self._phase == "build_label_buckets":
            self._master_by_label = {}
            self._candidate_by_label = {}
            for key, label in self._master_labels.items():
                self._master_by_label.setdefault(label, []).append(key)
            for key, label in self._candidate_labels.items():
                self._candidate_by_label.setdefault(label, []).append(key)
            if set(self._master_by_label) != set(self._candidate_by_label) or any(
                len(self._master_by_label[label]) != len(self._candidate_by_label[label])
                for label in self._master_by_label
            ):
                self._finish_rejected("topology_signature_mismatch")
                return
            self._refinement_pre_max_domain = max(
                [
                    len(values)
                    for values in tuple(self._master_by_label.values())
                    + tuple(self._candidate_by_label.values())
                ]
                or [0]
            )
            assert self._candidate_graph is not None
            self._domain_keys = tuple(self._candidate_graph.loops)
            self._domain_index = 0
            self._phase = "build_domains"
            return

        if self._phase == "build_domains":
            assert self._candidate_graph is not None
            if self._domain_index >= len(self._domain_keys):
                self._initial_domains = tuple(
                    (key, len(self._domains[key]))
                    for key in sorted(self._domains, key=_stable_loop_key)
                )
                self._refinement_post_max_domain = max(
                    (len(values) for values in self._domains.values()),
                    default=0,
                )
                self._phase = "prepare_search"
                return
            candidate_key = self._domain_keys[self._domain_index]
            label = self._candidate_labels[candidate_key]
            self._domains[candidate_key] = tuple(
                sorted(self._master_by_label[label], key=_stable_loop_key)
            )
            self._domain_index += 1
            return

        if self._phase == "prepare_search":
            assert self._candidate_graph is not None
            if any(not values for values in self._domains.values()):
                self._finish_rejected("topology_signature_mismatch")
                return
            self._search_order = tuple(
                sorted(
                    self._candidate_graph.loops,
                    key=lambda key: (
                        len(self._domains[key]),
                        0 if self._candidate_graph.loops[key].boundary else 1,
                        _stable_loop_key(key),
                    ),
                )
            )
            self._mapping = {}
            self._used_master = set()
            self._frames = [{"index": 0, "next": 0, "assigned": None}]
            self._master_points_by_key = {
                key: self._master_graph.loops[key].uv
                for key in self._master_graph.loops
            }
            self._candidate_points_by_key = {
                key: self._candidate_graph.loops[key].uv
                for key in self._candidate_graph.loops
            }
            self._phase = "search"
            return

        if self._phase == "search":
            self._advance_search_one()
            return

        if self._phase not in {"done", "cancelled"}:
            raise RuntimeError("unknown correspondence phase: %s" % self._phase)

    def _as_result(
        self,
        ordered_pairs: Tuple[Tuple[LoopKey, LoopKey], ...],
        face_orientation: Mapping[NodeKey, Tuple[int, int]],
        item: Tuple[float, float, bool, SimilarityTransform2D],
    ) -> CorrespondenceResult:
        score, residual, reflected, transform = item
        primary_face_key = sorted(face_orientation, key=_stable_key)[0]
        direction, shift = face_orientation[primary_face_key]
        reversed_flag = direction < 0
        effective_reflected = bool(reflected) ^ reversed_flag
        return CorrespondenceResult(
            accepted=residual <= self.tolerance
            and (self.allow_flipping or not effective_reflected),
            loop_mapping=ordered_pairs,
            reflected=effective_reflected,
            reversed=reversed_flag,
            cyclic_shift=shift,
            score=score,
            residual=residual,
            reason="accepted" if residual <= self.tolerance else "residual_above_tolerance",
            transform=transform,
            diagnostics=self._current_diagnostics(),
        )

    def _evaluate_complete(self) -> None:
        assert self._master_graph is not None
        assert self._candidate_graph is not None
        self._state["complete_mappings"] += 1
        verified = _verify_full_mapping(
            self._candidate_graph,
            self._master_graph,
            self._mapping,
        )
        self._state["topology_checks"] += 1
        if verified is None:
            self._state["pruned_count"] += 1
            return
        face_orientation, _ = verified
        ordered_pairs = tuple(
            sorted(self._mapping.items(), key=lambda item: _stable_loop_key(item[0]))
        )
        source_points = [self._candidate_points_by_key[key] for key, _ in ordered_pairs]
        target_points = [self._master_points_by_key[value] for _, value in ordered_pairs]
        fits: List[Tuple[float, float, bool, SimilarityTransform2D]] = []
        for reflected in (False, True):
            fitted = _fit_similarity(
                source_points,
                target_points,
                reflected,
                self.match_scale,
            )
            if fitted is None:
                continue
            transform, residual = fitted
            fits.append(
                (residual / _uv_span(target_points), residual, reflected, transform)
            )

        for item in fits:
            result = self._as_result(ordered_pairs, face_orientation, item)
            result_key = (
                result.residual,
                result.loop_mapping,
                1 if result.reflected else 0,
            )
            if self._best_fit is None or result_key < (
                self._best_fit.residual,
                self._best_fit.loop_mapping,
                1 if self._best_fit.reflected else 0,
            ):
                self._best_fit = result
            if result.residual <= self.tolerance and result.reflected and not self.allow_flipping:
                if self._best_disallowed_reflection is None or result_key < (
                    self._best_disallowed_reflection.residual,
                    self._best_disallowed_reflection.loop_mapping,
                    1,
                ):
                    self._best_disallowed_reflection = result
            if result.accepted:
                if self._best_accepted is None or result_key < (
                    self._best_accepted.residual,
                    self._best_accepted.loop_mapping,
                    1 if self._best_accepted.reflected else 0,
                ):
                    self._best_accepted = result

    def _advance_search_one(self) -> None:
        if not self._frames:
            final_diagnostics = self._current_diagnostics()
            if self._best_accepted is not None:
                selected = self._best_accepted
                self._finish(
                    CorrespondenceResult(
                        accepted=True,
                        loop_mapping=selected.loop_mapping,
                        reflected=selected.reflected,
                        reversed=selected.reversed,
                        cyclic_shift=selected.cyclic_shift,
                        score=selected.score,
                        residual=selected.residual,
                        reason="accepted",
                        transform=selected.transform,
                        diagnostics=final_diagnostics,
                    )
                )
            elif self._best_disallowed_reflection is not None and not self.allow_flipping:
                self._finish(_rejected("reflection_not_allowed", final_diagnostics))
            elif self._best_fit is not None:
                self._finish(_rejected("residual_above_tolerance", final_diagnostics))
            else:
                self._finish(_rejected("no_exact_bijection", final_diagnostics))
            return

        frame = self._frames[-1]
        index = frame["index"]
        if index == len(self._search_order):
            self._evaluate_complete()
            self._pop_search_frame()
            return

        candidate_key = self._search_order[index]
        domain = self._domains[candidate_key]
        if frame["next"] >= len(domain):
            self._pop_search_frame()
            return

        master_key = domain[frame["next"]]
        frame["next"] += 1
        self._state["search_count"] += 1
        if (
            self.cooperative_yield_every > 0
            and self._state["search_count"] % self.cooperative_yield_every == 0
        ):
            # Compatibility-only scheduling hook.  Pro uses the resumable
            # engine with interval zero and yields by returning from step().
            time.sleep(0)
            self._state["yield_count"] += 1
        if self._state["search_count"] > self.max_search:
            self._finish_rejected("search_budget_exceeded")
            return
        if master_key in self._used_master:
            self._state["pruned_count"] += 1
            return
        assert self._candidate_graph is not None
        assert self._master_graph is not None
        if not _partial_compatible(
            self._candidate_graph,
            self._master_graph,
            self._mapping,
            candidate_key,
            master_key,
        ):
            self._state["pruned_count"] += 1
            return
        self._mapping[candidate_key] = master_key
        self._used_master.add(master_key)
        self._frames.append(
            {"index": index + 1, "next": 0, "assigned": (candidate_key, master_key)}
        )

    def _pop_search_frame(self) -> None:
        frame = self._frames.pop()
        assigned = frame.get("assigned")
        if assigned is not None:
            candidate_key, master_key = assigned
            self._used_master.remove(master_key)
            del self._mapping[candidate_key]

    def step(
        self,
        *,
        deadline: Optional[float] = None,
        operation_budget: Optional[int] = None,
    ) -> CorrespondenceStep:
        """Advance at most until ``deadline`` or ``operation_budget``.

        A synchronous caller omits both limits and therefore drives this same
        engine to completion in one call.  Modal callers pass an absolute
        monotonic deadline and a hard operation cap.
        """

        if self.done:
            return CorrespondenceStep(
                status="cancelled" if self._cancelled else (
                    "success" if self._result and self._result.accepted else "failure"
                ),
                result=self._result,
                diagnostics=self._diagnostics,
                phase=self._phase,
            )
        if operation_budget is not None:
            try:
                operation_budget = max(0, int(operation_budget))
            except (TypeError, ValueError):
                operation_budget = 0

        started = time.perf_counter()
        operations = 0
        search_elapsed = 0.0
        while not self.done:
            if operation_budget is not None and operations >= operation_budget:
                break
            if deadline is not None and time.perf_counter() >= deadline:
                break
            phase = self._phase
            operation_started = time.perf_counter()
            self._advance_one()
            operation_elapsed = time.perf_counter() - operation_started
            if phase == "search" or self._phase == "search":
                search_elapsed += operation_elapsed * 1000.0
                self._search_operations_total += 1
            operations += 1
            self._operations_total += 1

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._diagnostics = (
            self._result.diagnostics if self._result is not None else self._current_diagnostics()
        )
        status = "pending"
        if self._cancelled:
            status = "cancelled"
        elif self._result is not None:
            status = "success" if self._result.accepted else "failure"
        self._last_step = CorrespondenceStep(
            status=status,
            result=self._result,
            diagnostics=self._diagnostics,
            operations=operations,
            elapsed_ms=elapsed_ms,
            search_elapsed_ms=search_elapsed,
            phase=self._phase,
        )
        return self._last_step


def find_correspondence(
    master: IslandGraph,
    candidate: IslandGraph,
    *,
    allow_flipping: bool = False,
    match_scale: bool = True,
    tolerance: float = 1.0e-6,
    max_search: int = 100000,
    cooperative_yield_every: int = 0,
) -> CorrespondenceResult:
    """Drive :class:`CorrespondenceSearch` to completion synchronously."""

    search = CorrespondenceSearch(
        master,
        candidate,
        allow_flipping=allow_flipping,
        match_scale=match_scale,
        tolerance=tolerance,
        max_search=max_search,
        cooperative_yield_every=cooperative_yield_every,
    )
    while not search.done:
        search.step()
    assert search.result is not None
    return search.result


find_exact_correspondence = find_correspondence


__all__ = [
    "BoundaryComponentRecord",
    "CorrespondenceDiagnostics",
    "CorrespondenceResult",
    "CorrespondenceSearch",
    "CorrespondenceStep",
    "EdgeRecord",
    "FaceRecord",
    "IslandGraph",
    "LoopKey",
    "LoopRecord",
    "SimilarityTransform2D",
    "VertexRecord",
    "find_correspondence",
    "find_exact_correspondence",
    "make_graph",
]
