"""Immutable, wire-only payloads for the external Pro correspondence pool.

The public records in this module are frozen and pure.  ``to_wire`` methods
convert them to primitives before pickle serialization, which keeps direct
file-path workers independent from package import names and Blender state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import math
from pathlib import Path
import pickle
from typing import Any, Iterable, Mapping, Optional


SCHEMA_VERSION = "uv-gpt-pro-payload-v1"
ALGORITHM_VERSION = "topology-correspondence-v1"
PICKLE_PROTOCOL = 5
MAX_FRAME_BYTES = 16 * 1024 * 1024
FRAME_PREFIX_BYTES = 8
FRAME_HEADER_BYTES = 79
MAX_IDENTIFIER_BYTES = 1024
PRODUCTION_BATCH_SIZES = (32, 64, 96)
CORRESPONDENCE_MODE_HYBRID = "HYBRID"
CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY = "VERIFIED_NEAREST_ONLY"
CORRESPONDENCE_MODE_EXACT_ONLY = "EXACT_ONLY"
CORRESPONDENCE_MODES = frozenset(
    {
        CORRESPONDENCE_MODE_HYBRID,
        CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY,
        CORRESPONDENCE_MODE_EXACT_ONLY,
    }
)


class PayloadValidationError(ValueError):
    """A payload, graph, result or identity violates the MC2 contract."""


class FrameSizeError(PayloadValidationError):
    """A serialized task cannot fit the finite protocol frame limit."""


def normalize_correspondence_mode(value: Any) -> str:
    """Return one explicit worker algorithm mode or reject the payload."""

    mode = str(value or CORRESPONDENCE_MODE_HYBRID).strip().upper()
    if mode not in CORRESPONDENCE_MODES:
        raise PayloadValidationError("unknown correspondence mode: %s" % mode)
    return mode


def _canonical(value: Any) -> Any:
    """Return insertion-order-independent primitive data for hashing."""

    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int) and not isinstance(value, bool):
        return ("int", str(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PayloadValidationError("non-finite value cannot be canonicalized")
        return ("float", value.hex())
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    if isinstance(value, Mapping):
        entries = [(_canonical(key), _canonical(item)) for key, item in value.items()]
        return ("map", tuple(sorted(entries, key=repr)))
    if isinstance(value, (tuple, list)):
        return ("seq", tuple(_canonical(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted((_canonical(item) for item in value), key=repr)))
    raise PayloadValidationError(f"unsupported payload value: {type(value).__name__}")


def stable_digest(value: Any) -> str:
    canonical = _canonical(value)
    encoded = pickle.dumps(canonical, protocol=PICKLE_PROTOCOL)
    return hashlib.sha256(encoded).hexdigest().upper()


def _validate_primitive_tree(value: Any) -> None:
    """Validate a large primitive wire tree without sorting/canonicalizing it."""

    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, (bool, int, str, bytes)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise PayloadValidationError("non-finite primitive value")
            continue
        if isinstance(item, Mapping):
            for key, child in item.items():
                pending.append(key)
                pending.append(child)
            continue
        if isinstance(item, (tuple, list, set, frozenset)):
            pending.extend(item)
            continue
        raise PayloadValidationError(
            f"unsupported primitive value: {type(item).__name__}"
        )


def graph_context_material_digest(value: Any) -> str:
    """Hash one already-canonical primitive context in linear time.

    The regular ``stable_digest`` intentionally canonicalizes arbitrary nested
    mappings and sets for general payloads.  A graph context is already a
    canonical tuple wire record, so sorting every nested primitive on the
    Blender main thread would turn a 12 MB snapshot into a multi-minute
    blocking operation.  Protocol-5 bytes preserve the tuple order established
    by the immutable snapshot and give the same digest to every worker.
    """

    _validate_primitive_tree(value)
    try:
        encoded = pickle.dumps(("graph-context-material-v1", value), protocol=PICKLE_PROTOCOL)
    except Exception as exc:
        raise PayloadValidationError("graph context material is not serializable") from exc
    return hashlib.sha256(encoded).hexdigest().upper()


def _stable_sort_key(value: Any) -> str:
    return repr(_canonical(value))


def _require_text(value: Any, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise PayloadValidationError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise PayloadValidationError(f"{name} is too large")
    return value


def _require_uint(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise PayloadValidationError(f"{name} must be an unsigned integer")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise PayloadValidationError(f"{name} must be bool")
    return value


def _require_finite(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadValidationError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        raise PayloadValidationError(f"{name} must be finite")
    return result


def _seed_transform_wire(value: Any, name: str = "seed_transform") -> Optional[tuple[Any, ...]]:
    """Normalize an optional candidate-to-master similarity seed to primitives."""

    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if len(value) != 5:
            raise PayloadValidationError(f"{name} must contain five values")
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
    angle = _require_finite(angle, f"{name}.angle")
    scale = _require_finite(scale, f"{name}.scale", positive=True)
    if not isinstance(reflected, bool):
        raise PayloadValidationError(f"{name}.reflected must be bool")
    for center_name, center in (("source_center", source_center), ("target_center", target_center)):
        if not isinstance(center, (tuple, list)) or len(center) != 2:
            raise PayloadValidationError(f"{name}.{center_name} must contain two values")
        _require_finite(center[0], f"{name}.{center_name}.x")
        _require_finite(center[1], f"{name}.{center_name}.y")
    return (
        angle,
        scale,
        reflected,
        (float(source_center[0]), float(source_center[1])),
        (float(target_center[0]), float(target_center[1])),
    )


@dataclass(frozen=True)
class SnapshotIdentity:
    session_nonce: str
    generation: int
    snapshot_digest: str
    schema_version: str = SCHEMA_VERSION
    algorithm_version: str = ALGORITHM_VERSION

    def __post_init__(self) -> None:
        _require_text(self.session_nonce, "session_nonce")
        _require_uint(self.generation, "generation")
        _require_text(self.snapshot_digest, "snapshot_digest")
        _require_text(self.schema_version, "schema_version")
        _require_text(self.algorithm_version, "algorithm_version")

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.schema_version,
            self.algorithm_version,
            self.session_nonce,
            self.generation,
            self.snapshot_digest,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "SnapshotIdentity":
        if not isinstance(value, (tuple, list)) or len(value) != 5:
            raise PayloadValidationError("invalid snapshot identity wire value")
        return cls(
            session_nonce=value[2],
            generation=value[3],
            snapshot_digest=value[4],
            schema_version=value[0],
            algorithm_version=value[1],
        )


GRAPH_CONTEXT_IDENTITY_DIGEST_TAG = "mc4c11-graph-context-identity-v1"


def graph_context_identity_digest(identity: SnapshotIdentity) -> str:
    """Return the O(1) identity for a resident graph context.

    ``SnapshotIdentity.snapshot_digest`` is already the authoritative digest of
    the immutable snapshot.  The context identity only needs to bind that
    digest to the payload/algorithm schema and generation; it must not walk or
    pickle the 12 MB topology again on Blender's modal thread.
    """

    if not isinstance(identity, SnapshotIdentity):
        raise PayloadValidationError("graph context identity is invalid")
    return stable_digest(
        (
            GRAPH_CONTEXT_IDENTITY_DIGEST_TAG,
            identity.schema_version,
            identity.algorithm_version,
            identity.generation,
            identity.snapshot_digest,
        )
    )


@dataclass(frozen=True)
class ExactOptions:
    allow_flipping: bool = False
    match_scale: bool = True
    tolerance: float = 1.0e-6
    max_search: int = 100000
    cooperative_yield_every: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.allow_flipping, bool) or not isinstance(self.match_scale, bool):
            raise PayloadValidationError("boolean exact options are invalid")
        _require_finite(self.tolerance, "tolerance", positive=True)
        _require_uint(self.max_search, "max_search", positive=True)
        _require_uint(self.cooperative_yield_every, "cooperative_yield_every")

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.allow_flipping,
            self.match_scale,
            self.tolerance,
            self.max_search,
            self.cooperative_yield_every,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ExactOptions":
        if not isinstance(value, (tuple, list)) or len(value) != 5:
            raise PayloadValidationError("invalid exact options wire value")
        return cls(*value)


@dataclass(frozen=True)
class GraphRef:
    graph_key: str
    content_digest: str

    def __post_init__(self) -> None:
        _require_text(self.graph_key, "graph_key")
        _require_text(self.content_digest, "content_digest")

    def to_wire(self) -> tuple[str, str]:
        return (self.graph_key, self.content_digest)

    @classmethod
    def from_wire(cls, value: Any) -> "GraphRef":
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise PayloadValidationError("invalid graph reference")
        return cls(value[0], value[1])


@dataclass(frozen=True)
class GraphLoopData:
    key: Any
    face_key: Any
    edge_key: Any
    vertex_key: Any
    next_key: Any
    prev_key: Any
    uv: tuple[float, float]
    boundary: bool = False
    seam: bool = False
    signature: tuple[Any, ...] = ()

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.key,
            self.face_key,
            self.edge_key,
            self.vertex_key,
            self.next_key,
            self.prev_key,
            tuple(self.uv),
            self.boundary,
            self.seam,
            tuple(self.signature),
        )

    @classmethod
    def from_wire(cls, value: Any) -> "GraphLoopData":
        if not isinstance(value, (tuple, list)) or len(value) != 10:
            raise PayloadValidationError("invalid graph loop wire value")
        return cls(
            value[0], value[1], value[2], value[3], value[4], value[5],
            tuple(value[6]), bool(value[7]), bool(value[8]), tuple(value[9]),
        )


@dataclass(frozen=True)
class GraphFaceData:
    key: Any
    loop_keys: tuple[Any, ...]
    signature: tuple[Any, ...] = ()

    def to_wire(self) -> tuple[Any, ...]:
        return (self.key, tuple(self.loop_keys), tuple(self.signature))

    @classmethod
    def from_wire(cls, value: Any) -> "GraphFaceData":
        if not isinstance(value, (tuple, list)) or len(value) != 3:
            raise PayloadValidationError("invalid graph face wire value")
        return cls(value[0], tuple(value[1]), tuple(value[2]))


@dataclass(frozen=True)
class GraphEdgeData:
    key: Any
    loop_keys: tuple[Any, ...]
    face_keys: tuple[Any, ...]
    boundary: bool = False
    non_manifold: bool = False
    signature: tuple[Any, ...] = ()

    def to_wire(self) -> tuple[Any, ...]:
        return (
            self.key,
            tuple(self.loop_keys),
            tuple(self.face_keys),
            self.boundary,
            self.non_manifold,
            tuple(self.signature),
        )

    @classmethod
    def from_wire(cls, value: Any) -> "GraphEdgeData":
        if not isinstance(value, (tuple, list)) or len(value) != 6:
            raise PayloadValidationError("invalid graph edge wire value")
        return cls(value[0], tuple(value[1]), tuple(value[2]), bool(value[3]), bool(value[4]), tuple(value[5]))


@dataclass(frozen=True)
class GraphVertexData:
    key: Any
    loop_keys: tuple[Any, ...]
    boundary: bool = False
    signature: tuple[Any, ...] = ()

    def to_wire(self) -> tuple[Any, ...]:
        return (self.key, tuple(self.loop_keys), self.boundary, tuple(self.signature))

    @classmethod
    def from_wire(cls, value: Any) -> "GraphVertexData":
        if not isinstance(value, (tuple, list)) or len(value) != 4:
            raise PayloadValidationError("invalid graph vertex wire value")
        return cls(value[0], tuple(value[1]), bool(value[2]), tuple(value[3]))


@dataclass(frozen=True)
class GraphBoundaryData:
    key: Any
    loop_keys: tuple[Any, ...]
    role: str = "outer"
    parent_key: Any = None
    signature: tuple[Any, ...] = ()

    def to_wire(self) -> tuple[Any, ...]:
        return (self.key, tuple(self.loop_keys), self.role, self.parent_key, tuple(self.signature))

    @classmethod
    def from_wire(cls, value: Any) -> "GraphBoundaryData":
        if not isinstance(value, (tuple, list)) or len(value) != 5:
            raise PayloadValidationError("invalid graph boundary wire value")
        return cls(value[0], tuple(value[1]), value[2], value[3], tuple(value[4]))


@dataclass(frozen=True)
class GraphData:
    graph_key: str
    faces: tuple[GraphFaceData, ...]
    edges: tuple[GraphEdgeData, ...]
    vertices: tuple[GraphVertexData, ...]
    loops: tuple[GraphLoopData, ...]
    boundaries: tuple[GraphBoundaryData, ...] = ()
    content_digest: str = ""

    def __post_init__(self) -> None:
        _require_text(self.graph_key, "graph_key")
        if not self.content_digest:
            object.__setattr__(self, "content_digest", self.computed_content_digest())
        _require_text(self.content_digest, "content_digest")

    def _content_wire(self) -> tuple[Any, ...]:
        return (
            self.graph_key,
            tuple(item.to_wire() for item in self.faces),
            tuple(item.to_wire() for item in self.edges),
            tuple(item.to_wire() for item in self.vertices),
            tuple(item.to_wire() for item in self.loops),
            tuple(item.to_wire() for item in self.boundaries),
        )

    def computed_content_digest(self) -> str:
        return stable_digest(self._content_wire())

    def validate(self) -> None:
        if self.content_digest != self.computed_content_digest():
            raise PayloadValidationError(f"graph digest mismatch: {self.graph_key}")
        _canonical(self.to_wire())
        if not self.loops or not self.faces:
            raise PayloadValidationError(f"graph is empty: {self.graph_key}")

    def to_wire(self) -> tuple[Any, ...]:
        return ("graph", self.graph_key, self.content_digest, self._content_wire()[1:])

    @classmethod
    def from_wire(cls, value: Any) -> "GraphData":
        if not isinstance(value, (tuple, list)) or len(value) != 4 or value[0] != "graph":
            raise PayloadValidationError("invalid graph data wire value")
        graph_key, digest, content = value[1], value[2], value[3]
        if not isinstance(content, (tuple, list)) or len(content) != 5:
            raise PayloadValidationError("invalid graph content wire value")
        return cls(
            graph_key=graph_key,
            content_digest=digest,
            faces=tuple(GraphFaceData.from_wire(item) for item in content[0]),
            edges=tuple(GraphEdgeData.from_wire(item) for item in content[1]),
            vertices=tuple(GraphVertexData.from_wire(item) for item in content[2]),
            loops=tuple(GraphLoopData.from_wire(item) for item in content[3]),
            boundaries=tuple(GraphBoundaryData.from_wire(item) for item in content[4]),
        )

    @classmethod
    def from_topology(cls, graph: Any, graph_key: str) -> "GraphData":
        def loop(record: Any) -> GraphLoopData:
            return GraphLoopData(
                record.key, record.face_key, record.edge_key, record.vertex_key,
                record.next_key, record.prev_key, tuple(record.uv), bool(record.boundary),
                bool(record.seam), tuple(record.signature),
            )

        def face(record: Any) -> GraphFaceData:
            return GraphFaceData(record.key, tuple(record.loop_keys), tuple(record.signature))

        def edge(record: Any) -> GraphEdgeData:
            return GraphEdgeData(
                record.key, tuple(record.loop_keys), tuple(record.face_keys), bool(record.boundary),
                bool(record.non_manifold), tuple(record.signature),
            )

        def vertex(record: Any) -> GraphVertexData:
            return GraphVertexData(record.key, tuple(record.loop_keys), bool(record.boundary), tuple(record.signature))

        def boundary(record: Any) -> GraphBoundaryData:
            return GraphBoundaryData(
                record.key, tuple(record.loop_keys), record.role, record.parent_key, tuple(record.signature)
            )

        result = cls(
            graph_key=graph_key,
            faces=tuple(sorted((face(item) for item in graph.faces), key=lambda item: _stable_sort_key(item.key))),
            edges=tuple(sorted((edge(item) for item in graph.edges), key=lambda item: _stable_sort_key(item.key))),
            vertices=tuple(sorted((vertex(item) for item in graph.vertices), key=lambda item: _stable_sort_key(item.key))),
            loops=tuple(sorted((loop(item) for item in graph.loops), key=lambda item: _stable_sort_key(item.key))),
            boundaries=tuple(sorted((boundary(item) for item in graph.boundaries), key=lambda item: _stable_sort_key(item.key))),
        )
        result.validate()
        return result

    def to_topology_graph(self, topology_module: Any = None) -> Any:
        if topology_module is None:
            try:
                import topology_correspondence as topology_module  # type: ignore[no-redef]
            except ImportError:
                path = Path(__file__).with_name("topology_correspondence.py")
                spec = importlib.util.spec_from_file_location("pro_process_topology", path)
                if spec is None or spec.loader is None:
                    raise PayloadValidationError("cannot load pure topology module")
                topology_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(topology_module)
        self.validate()
        return topology_module.make_graph(
            faces=tuple(topology_module.FaceRecord(item.key, item.loop_keys, item.signature) for item in self.faces),
            edges=tuple(
                topology_module.EdgeRecord(
                    item.key, item.loop_keys, item.face_keys, item.boundary, item.non_manifold, item.signature
                )
                for item in self.edges
            ),
            vertices=tuple(
                topology_module.VertexRecord(item.key, item.loop_keys, item.boundary, item.signature)
                for item in self.vertices
            ),
            loops=tuple(
                topology_module.LoopRecord(
                    item.key, item.face_key, item.edge_key, item.vertex_key, item.next_key,
                    item.prev_key, item.uv, item.boundary, item.seam, item.signature
                )
                for item in self.loops
            ),
            boundaries=tuple(
                topology_module.BoundaryComponentRecord(
                    item.key, item.loop_keys, item.role, item.parent_key, item.signature
                )
                for item in self.boundaries
            ),
        )


GRAPH_BUILD_OPERATION = "snapshot_graph_build_batch"
GRAPH_BUILD_RESULT_OPERATION = "snapshot_graph_build_result"
GRAPH_CONTEXT_OPERATION = "snapshot_graph_context_load"
GRAPH_CONTEXT_ACK_OPERATION = "snapshot_graph_context_ack"
RESIDENT_EXACT_OPERATION = "resident_exact_correspondence_batch"
RESIDENT_EXACT_RESULT_OPERATION = "resident_exact_correspondence_result"
FUSED_CONTEXT_VERSION = "uv-gpt-fused-context-v1"


def graph_context_item_digest(context_digest: str, island_key: Any) -> str:
    """Stable identity for one island slice in a resident graph context."""

    _require_text(context_digest, "graph context digest")
    return stable_digest(("graph-context-item-v1", context_digest, island_key))


def fused_context_digest(
    context_digest: str,
    descriptor_digests: Iterable[str],
    shape_options: Any,
) -> str:
    """Return the compact identity for the resident fused shape context.

    The topology context digest remains the authoritative snapshot identity.
    This secondary digest covers only the immutable descriptor references and
    matcher options; it deliberately does not canonicalize the full 12 MB
    topology again on the Blender main thread.
    """

    _require_text(context_digest, "fused context topology digest")
    values = tuple(sorted(str(item) for item in descriptor_digests))
    return stable_digest((FUSED_CONTEXT_VERSION, context_digest, values, shape_options))


@dataclass(frozen=True)
class GraphContextPayload:
    """One immutable primitive topology context loaded once per worker."""

    identity: SnapshotIdentity
    context_digest: str
    material: Any
    # Optional immutable shape context.  Keeping these fields on the existing
    # context/ACK record lets the pool preserve its one-context lifecycle and
    # lets older graph-only callers continue to use the same wire shape.
    fused_descriptors: tuple[Any, ...] = ()
    fused_shape_options: Any = None
    fused_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("graph context identity is invalid")
        _require_text(self.context_digest, "graph context digest")
        if graph_context_identity_digest(self.identity) != self.context_digest:
            raise PayloadValidationError("graph context identity digest mismatch")
        object.__setattr__(self, "fused_descriptors", tuple(self.fused_descriptors or ()))
        if self.fused_descriptors or self.fused_shape_options is not None:
            _require_text(self.fused_digest, "fused context digest")
            descriptor_digests = []
            for item in self.fused_descriptors:
                if not isinstance(item, Mapping):
                    raise PayloadValidationError("fused descriptor wire value is invalid")
                digest = item.get("digest")
                _require_text(digest, "fused descriptor digest")
                descriptor_digests.append(digest)
            expected = fused_context_digest(
                self.context_digest,
                descriptor_digests,
                self.fused_shape_options,
            )
            if expected != self.fused_digest:
                raise PayloadValidationError("fused context digest mismatch")
            _validate_primitive_tree(self.fused_descriptors)
            _validate_primitive_tree(self.fused_shape_options)
        elif self.fused_digest:
            raise PayloadValidationError("fused context digest has no fused payload")

    def to_wire(self) -> dict[str, Any]:
        result = {
            "operation": GRAPH_CONTEXT_OPERATION,
            "identity": self.identity.to_wire(),
            "context_digest": self.context_digest,
            "material": self.material,
        }
        if self.fused_descriptors or self.fused_shape_options is not None:
            result.update({
                "fused_version": FUSED_CONTEXT_VERSION,
                "fused_descriptors": tuple(self.fused_descriptors),
                "fused_shape_options": self.fused_shape_options,
                "fused_digest": self.fused_digest,
            })
        return result

    @classmethod
    def from_wire(cls, value: Any) -> "GraphContextPayload":
        if not isinstance(value, Mapping) or value.get("operation") != GRAPH_CONTEXT_OPERATION:
            raise PayloadValidationError("invalid graph context operation")
        context = cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            context_digest=value["context_digest"],
            material=value["material"],
            fused_descriptors=tuple(value.get("fused_descriptors", ())),
            fused_shape_options=value.get("fused_shape_options"),
            fused_digest=value.get("fused_digest", ""),
        )
        return context

    def estimate_frame(self, *, batch_id: str = "") -> "SerializationEstimate":
        payload = pickle.dumps(self.to_wire(), protocol=PICKLE_PROTOCOL)
        if len(payload) > MAX_FRAME_BYTES:
            raise FrameSizeError("serialized graph context exceeds MAX_FRAME_BYTES")
        nonce_bytes = self.identity.session_nonce.encode("utf-8")
        batch_bytes = str(batch_id).encode("utf-8")
        frame_bytes = FRAME_PREFIX_BYTES + FRAME_HEADER_BYTES + len(nonce_bytes) + len(batch_bytes) + len(payload)
        if frame_bytes > MAX_FRAME_BYTES:
            raise FrameSizeError("graph context frame exceeds MAX_FRAME_BYTES")
        return SerializationEstimate(
            payload_bytes=len(payload),
            frame_bytes=frame_bytes,
            payload_digest=hashlib.sha256(payload).hexdigest().upper(),
        )


@dataclass(frozen=True)
class GraphContextLoadTask:
    """Internal control task used to load a context into one worker."""

    identity: SnapshotIdentity
    batch_id: str
    context: GraphContextPayload

    def __post_init__(self) -> None:
        if self.identity != self.context.identity:
            raise PayloadValidationError("graph context task identity mismatch")
        _require_text(self.batch_id, "graph context batch_id")

    @property
    def operation_kind(self) -> str:
        return "graph_context"

    @property
    def item_count(self) -> int:
        return 0

    @property
    def pair_tasks(self) -> tuple[Any, ...]:
        return ()

    @property
    def pair_ordinals(self) -> tuple[int, ...]:
        return ()

    def to_wire(self) -> dict[str, Any]:
        return self.context.to_wire()

    def validate(self) -> None:
        if self.identity.session_nonce != self.context.identity.session_nonce:
            raise PayloadValidationError("graph context task session mismatch")
        if self.identity.generation != self.context.identity.generation:
            raise PayloadValidationError("graph context task generation mismatch")

    def estimate_frame(self) -> "SerializationEstimate":
        self.validate()
        return self.context.estimate_frame(batch_id=self.batch_id)

    @staticmethod
    def result_from_wire(value: Any) -> "GraphContextLoadResult":
        return GraphContextLoadResult.from_wire(value)


@dataclass(frozen=True)
class GraphContextLoadResult:
    identity: SnapshotIdentity
    batch_id: str
    context_digest: str
    complete: bool = True
    load_ms: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("graph context result identity is invalid")
        _require_text(self.batch_id, "graph context result batch_id")
        _require_text(self.context_digest, "graph context result digest")
        _require_bool(self.complete, "graph context result complete")
        _require_finite(self.load_ms, "graph context result load_ms")

    def validate_against(self, task: GraphContextLoadTask) -> None:
        task.validate()
        if not self.complete:
            raise PayloadValidationError("incomplete graph context ACK")
        if self.identity != task.identity or self.batch_id != task.batch_id:
            raise PayloadValidationError("graph context ACK identity mismatch")
        if self.context_digest != task.context.context_digest:
            raise PayloadValidationError("graph context ACK digest mismatch")

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": GRAPH_CONTEXT_ACK_OPERATION,
            "identity": self.identity.to_wire(),
            "batch_id": self.batch_id,
            "context_digest": self.context_digest,
            "complete": self.complete,
            "load_ms": self.load_ms,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "GraphContextLoadResult":
        if not isinstance(value, Mapping) or value.get("operation") != GRAPH_CONTEXT_ACK_OPERATION:
            raise PayloadValidationError("invalid graph context ACK operation")
        return cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            batch_id=value["batch_id"],
            context_digest=value["context_digest"],
            complete=value.get("complete", False),
            load_ms=value.get("load_ms", 0.0),
        )


@dataclass(frozen=True)
class GraphBuildItem:
    """One immutable island-material request inside a graph build batch."""

    island_key: Any
    material_digest: str
    material: Any = None

    def __post_init__(self) -> None:
        _canonical(self.island_key)
        _require_text(self.material_digest, "graph material digest")
        if self.material is not None:
            if stable_digest(self.material) != self.material_digest:
                raise PayloadValidationError("graph material digest mismatch")
            _canonical(self.material)

    def to_wire(self) -> tuple[Any, ...]:
        return ("graph-item", self.island_key, self.material_digest, self.material)

    @classmethod
    def from_wire(cls, value: Any) -> "GraphBuildItem":
        if not isinstance(value, (tuple, list)) or len(value) != 4 or value[0] != "graph-item":
            raise PayloadValidationError("invalid graph build item wire value")
        return cls(value[1], value[2], value[3])


@dataclass(frozen=True)
class GraphBuildTask:
    """Typed pure graph-build work sent through the existing worker pool.

    A task may carry more than one island so a master/member pair can be
    deduplicated and built in one bounded admission slot.  It intentionally
    exposes an empty pair-ordinal view: graph work is an intermediate typed
    operation, never a consumable correspondence result.
    """

    identity: SnapshotIdentity
    batch_id: str
    graph_items: tuple[GraphBuildItem, ...]
    debug_delay_ms: int = 0
    context_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("graph task identity is invalid")
        _require_text(self.batch_id, "graph batch_id")
        if not self.graph_items:
            raise PayloadValidationError("graph build batch must contain an item")
        ordered = tuple(sorted(self.graph_items, key=lambda item: _stable_sort_key(item.island_key)))
        if len({_stable_sort_key(item.island_key) for item in ordered}) != len(ordered):
            raise PayloadValidationError("graph build batch contains duplicate island key")
        object.__setattr__(self, "graph_items", ordered)
        if self.context_digest:
            _require_text(self.context_digest, "graph task context digest")
            for item in ordered:
                if item.material is not None:
                    raise PayloadValidationError("context graph task cannot carry island material")
                if item.material_digest != graph_context_item_digest(self.context_digest, item.island_key):
                    raise PayloadValidationError("context graph item digest mismatch")
        elif any(item.material is None for item in ordered):
            raise PayloadValidationError("legacy graph task requires island material")
        if isinstance(self.debug_delay_ms, bool) or not isinstance(self.debug_delay_ms, int) or not 0 <= self.debug_delay_ms <= 10000:
            raise PayloadValidationError("debug_delay_ms is out of bounds")

    @property
    def operation_kind(self) -> str:
        return "graph"

    @property
    def pair_tasks(self) -> tuple[Any, ...]:
        return ()

    @property
    def pair_ordinals(self) -> tuple[int, ...]:
        return ()

    @property
    def item_count(self) -> int:
        return len(self.graph_items)

    @property
    def island_keys(self) -> tuple[Any, ...]:
        return tuple(item.island_key for item in self.graph_items)

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": GRAPH_BUILD_OPERATION,
            "identity": self.identity.to_wire(),
            "batch_id": self.batch_id,
            "graph_items": tuple(item.to_wire() for item in self.graph_items),
            "debug_delay_ms": self.debug_delay_ms,
            "context_digest": self.context_digest,
        }

    def validate(self) -> None:
        _canonical(self.to_wire())

    def payload_digest(self) -> str:
        return stable_digest(self.to_wire())

    def estimate_frame(self) -> "SerializationEstimate":
        payload = pickle.dumps(self.to_wire(), protocol=PICKLE_PROTOCOL)
        if len(payload) > MAX_FRAME_BYTES:
            raise FrameSizeError("serialized graph build payload exceeds MAX_FRAME_BYTES")
        nonce_bytes = self.identity.session_nonce.encode("utf-8")
        batch_bytes = self.batch_id.encode("utf-8")
        frame_bytes = FRAME_PREFIX_BYTES + FRAME_HEADER_BYTES + len(nonce_bytes) + len(batch_bytes) + len(payload)
        if frame_bytes > MAX_FRAME_BYTES:
            raise FrameSizeError("serialized graph build frame exceeds MAX_FRAME_BYTES")
        return SerializationEstimate(
            payload_bytes=len(payload),
            frame_bytes=frame_bytes,
            payload_digest=hashlib.sha256(payload).hexdigest().upper(),
        )

    @classmethod
    def from_wire(cls, value: Any) -> "GraphBuildTask":
        if not isinstance(value, Mapping) or value.get("operation") != GRAPH_BUILD_OPERATION:
            raise PayloadValidationError("invalid graph build operation")
        task = cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            batch_id=value["batch_id"],
            graph_items=tuple(GraphBuildItem.from_wire(item) for item in value["graph_items"]),
            debug_delay_ms=value.get("debug_delay_ms", 0),
            context_digest=value.get("context_digest", ""),
        )
        task.validate()
        return task

    @staticmethod
    def result_from_wire(value: Any) -> "GraphBuildResult":
        return GraphBuildResult.from_wire(value)


# The architecture calls the wire operation a task or a batch depending on
# the caller.  The alias keeps both names explicit without introducing a
# second schema or a second scheduling path.
GraphBuildBatch = GraphBuildTask


@dataclass(frozen=True)
class GraphBuildEntry:
    island_key: Any
    material_digest: str
    accepted: bool
    graph: Optional[GraphData] = None
    reason: str = ""
    graph_digest: str = ""
    complete: bool = True

    def __post_init__(self) -> None:
        _canonical(self.island_key)
        _require_text(self.material_digest, "graph result material digest")
        _require_bool(self.accepted, "graph result accepted")
        _require_bool(self.complete, "graph result complete")
        if self.graph is not None:
            self.graph.validate()
            digest = self.graph.content_digest
            if self.graph_digest and self.graph_digest != digest:
                raise PayloadValidationError("graph result digest mismatch")
            object.__setattr__(self, "graph_digest", digest)
        elif self.graph_digest:
            _require_text(self.graph_digest, "graph result graph digest")
        if not self.accepted and self.graph is not None:
            raise PayloadValidationError("rejected graph result cannot carry graph data")
        if self.accepted and self.graph is None:
            raise PayloadValidationError("accepted graph result lacks graph data")
        reason = str(self.reason)[:512]
        object.__setattr__(self, "reason", reason)
        _require_text(reason, "graph result reason", nonempty=False)

    def to_wire(self) -> tuple[Any, ...]:
        return (
            "graph-result",
            self.island_key,
            self.material_digest,
            self.accepted,
            None if self.graph is None else self.graph.to_wire(),
            self.reason,
            self.graph_digest,
            self.complete,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "GraphBuildEntry":
        if not isinstance(value, (tuple, list)) or len(value) != 8 or value[0] != "graph-result":
            raise PayloadValidationError("invalid graph result entry wire value")
        graph = None if value[4] is None else GraphData.from_wire(value[4])
        return cls(
            island_key=value[1],
            material_digest=value[2],
            accepted=value[3],
            graph=graph,
            reason=value[5],
            graph_digest=value[6],
            complete=value[7],
        )


@dataclass(frozen=True)
class GraphBuildResult:
    identity: SnapshotIdentity
    batch_id: str
    payload_digest: str
    graph_results: tuple[GraphBuildEntry, ...]
    complete: bool = True
    compute_ms: float = 0.0
    cache_hits: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("graph result identity is invalid")
        _require_text(self.batch_id, "graph result batch_id")
        _require_text(self.payload_digest, "graph result payload digest")
        _require_bool(self.complete, "graph result complete")
        _require_finite(self.compute_ms, "graph result compute_ms")
        if isinstance(self.cache_hits, bool) or not isinstance(self.cache_hits, int) or self.cache_hits < 0:
            raise PayloadValidationError("graph result cache_hits must be a non-negative integer")
        ordered = tuple(sorted(self.graph_results, key=lambda item: _stable_sort_key(item.island_key)))
        if len({_stable_sort_key(item.island_key) for item in ordered}) != len(ordered):
            raise PayloadValidationError("graph result contains duplicate island key")
        object.__setattr__(self, "graph_results", ordered)

    @property
    def pair_results(self) -> tuple[Any, ...]:
        return ()

    def validate_against(self, task: GraphBuildTask) -> None:
        task.validate()
        if not self.complete:
            raise PayloadValidationError("incomplete graph result is not consumable")
        if self.identity != task.identity or self.batch_id != task.batch_id:
            raise PayloadValidationError("graph result identity mismatch")
        if self.payload_digest != task.payload_digest():
            raise PayloadValidationError("graph result payload digest mismatch")
        if tuple(_stable_sort_key(item.island_key) for item in self.graph_results) != tuple(
            _stable_sort_key(item.island_key) for item in task.graph_items
        ):
            raise PayloadValidationError("graph result island coverage mismatch")
        expected = { _stable_sort_key(item.island_key): item for item in task.graph_items }
        for result in self.graph_results:
            item = expected.get(_stable_sort_key(result.island_key))
            if item is None or result.material_digest != item.material_digest:
                raise PayloadValidationError("graph result material identity mismatch")
            if not result.complete:
                raise PayloadValidationError("incomplete graph entry is not consumable")

    def result_digest(self) -> str:
        return stable_digest((self.identity.to_wire(), self.batch_id, self.payload_digest, tuple(item.to_wire() for item in self.graph_results)))

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": GRAPH_BUILD_RESULT_OPERATION,
            "identity": self.identity.to_wire(),
            "batch_id": self.batch_id,
            "payload_digest": self.payload_digest,
            "graph_results": tuple(item.to_wire() for item in self.graph_results),
            "complete": self.complete,
            "compute_ms": self.compute_ms,
            "cache_hits": self.cache_hits,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "GraphBuildResult":
        if not isinstance(value, Mapping) or value.get("operation") != GRAPH_BUILD_RESULT_OPERATION:
            raise PayloadValidationError("invalid graph result operation")
        return cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            batch_id=value["batch_id"],
            payload_digest=value["payload_digest"],
            graph_results=tuple(GraphBuildEntry.from_wire(item) for item in value["graph_results"]),
            complete=value.get("complete", False),
            compute_ms=value.get("compute_ms", 0.0),
            cache_hits=value.get("cache_hits", 0),
        )


@dataclass(frozen=True)
class PairTask:
    pair_ordinal: int
    master_key: Any
    member_key: Any
    master_graph: GraphRef
    member_graph: GraphRef
    options: ExactOptions = ExactOptions()
    correspondence_mode: str = CORRESPONDENCE_MODE_HYBRID

    def __post_init__(self) -> None:
        _require_uint(self.pair_ordinal, "pair_ordinal")
        _canonical(self.master_key)
        _canonical(self.member_key)
        object.__setattr__(
            self,
            "correspondence_mode",
            normalize_correspondence_mode(self.correspondence_mode),
        )

    def to_wire(self) -> tuple[Any, ...]:
        return (
            "pair",
            self.pair_ordinal,
            self.master_key,
            self.member_key,
            self.master_graph.to_wire(),
            self.member_graph.to_wire(),
            self.options.to_wire(),
            self.correspondence_mode,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "PairTask":
        if not isinstance(value, (tuple, list)) or len(value) not in (7, 8) or value[0] != "pair":
            raise PayloadValidationError("invalid pair task wire value")
        return cls(
            pair_ordinal=value[1],
            master_key=value[2],
            member_key=value[3],
            master_graph=GraphRef.from_wire(value[4]),
            member_graph=GraphRef.from_wire(value[5]),
            options=ExactOptions.from_wire(value[6]),
            correspondence_mode=(
                CORRESPONDENCE_MODE_HYBRID if len(value) == 7 else value[7]
            ),
        )


@dataclass(frozen=True)
class ResidentExactPair:
    """One exact pair whose graphs live in the worker context/cache."""

    pair_ordinal: int
    master_key: Any
    member_key: Any
    master_loop_keys: tuple[Any, ...]
    member_loop_keys: tuple[Any, ...]
    options: ExactOptions = ExactOptions()
    seed_transform: Any = None
    correspondence_mode: str = CORRESPONDENCE_MODE_HYBRID

    def __post_init__(self) -> None:
        _require_uint(self.pair_ordinal, "resident pair ordinal")
        _canonical(self.master_key)
        _canonical(self.member_key)
        for name, values in (
            ("master_loop_keys", self.master_loop_keys),
            ("member_loop_keys", self.member_loop_keys),
        ):
            if not isinstance(values, (tuple, list)):
                raise PayloadValidationError(f"{name} must be a sequence")
            normalised = tuple(values)
            if len({_stable_sort_key(item) for item in normalised}) != len(normalised):
                raise PayloadValidationError(f"{name} contains duplicate loop key")
            object.__setattr__(
                self,
                name,
                tuple(sorted(normalised, key=_stable_sort_key)),
            )
            _canonical(normalised)
        object.__setattr__(self, "seed_transform", _seed_transform_wire(self.seed_transform))
        object.__setattr__(
            self,
            "correspondence_mode",
            normalize_correspondence_mode(self.correspondence_mode),
        )

    def to_wire(self) -> tuple[Any, ...]:
        return (
            "resident-pair",
            self.pair_ordinal,
            self.master_key,
            self.member_key,
            tuple(self.master_loop_keys),
            tuple(self.member_loop_keys),
            self.options.to_wire(),
            self.seed_transform,
            self.correspondence_mode,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ResidentExactPair":
        if not isinstance(value, (tuple, list)) or len(value) not in (7, 8, 9) or value[0] != "resident-pair":
            raise PayloadValidationError("invalid resident exact pair wire value")
        return cls(
            pair_ordinal=value[1],
            master_key=value[2],
            member_key=value[3],
            master_loop_keys=tuple(value[4]),
            member_loop_keys=tuple(value[5]),
            options=ExactOptions.from_wire(value[6]),
            seed_transform=None if len(value) == 7 else value[7],
            correspondence_mode=(
                CORRESPONDENCE_MODE_HYBRID if len(value) < 9 else value[8]
            ),
        )


@dataclass(frozen=True)
class ResidentExactBatchTask:
    """Small exact task referring only to a worker-resident graph context."""

    identity: SnapshotIdentity
    context_digest: str
    batch_id: str
    pair_tasks: tuple[ResidentExactPair, ...]
    debug_delay_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("resident exact identity is invalid")
        _require_text(self.context_digest, "resident exact context digest")
        if graph_context_identity_digest(self.identity) != self.context_digest:
            raise PayloadValidationError("resident exact context identity mismatch")
        _require_text(self.batch_id, "resident exact batch_id")
        if not self.pair_tasks:
            raise PayloadValidationError("resident exact batch must contain a pair")
        ordered = tuple(sorted(self.pair_tasks, key=lambda item: item.pair_ordinal))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PayloadValidationError("resident exact batch contains duplicate ordinal")
        if any(not isinstance(item, ResidentExactPair) for item in ordered):
            raise PayloadValidationError("resident exact batch contains an invalid pair")
        object.__setattr__(self, "pair_tasks", ordered)
        if (
            isinstance(self.debug_delay_ms, bool)
            or not isinstance(self.debug_delay_ms, int)
            or not 0 <= self.debug_delay_ms <= 10000
        ):
            raise PayloadValidationError("debug_delay_ms is out of bounds")

    @property
    def operation_kind(self) -> str:
        return "resident_exact"

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

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": RESIDENT_EXACT_OPERATION,
            "identity": self.identity.to_wire(),
            "context_digest": self.context_digest,
            "batch_id": self.batch_id,
            "pairs": tuple(item.to_wire() for item in self.pair_tasks),
            "debug_delay_ms": self.debug_delay_ms,
        }

    def validate(self) -> None:
        _canonical(self.to_wire())

    def payload_digest(self) -> str:
        return stable_digest(self.to_wire())

    def estimate_frame(self) -> "SerializationEstimate":
        payload = pickle.dumps(self.to_wire(), protocol=PICKLE_PROTOCOL)
        if len(payload) > MAX_FRAME_BYTES:
            raise FrameSizeError("serialized resident exact payload exceeds MAX_FRAME_BYTES")
        nonce_bytes = self.identity.session_nonce.encode("utf-8")
        batch_bytes = self.batch_id.encode("utf-8")
        frame_bytes = FRAME_PREFIX_BYTES + FRAME_HEADER_BYTES + len(nonce_bytes) + len(batch_bytes) + len(payload)
        if frame_bytes > MAX_FRAME_BYTES:
            raise FrameSizeError("resident exact frame exceeds MAX_FRAME_BYTES")
        return SerializationEstimate(
            payload_bytes=len(payload),
            frame_bytes=frame_bytes,
            payload_digest=hashlib.sha256(payload).hexdigest().upper(),
        )

    def cache_key(self) -> "BatchCacheKey":
        return BatchCacheKey(
            schema_version=self.identity.schema_version,
            algorithm_version=self.identity.algorithm_version,
            generation=self.identity.generation,
            snapshot_digest=self.identity.snapshot_digest,
            pair_digest=stable_digest((self.context_digest, self.to_wire()["pairs"])),
        )

    @classmethod
    def from_wire(cls, value: Any) -> "ResidentExactBatchTask":
        if not isinstance(value, Mapping) or value.get("operation") != RESIDENT_EXACT_OPERATION:
            raise PayloadValidationError("invalid resident exact operation")
        task = cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            context_digest=value["context_digest"],
            batch_id=value["batch_id"],
            pair_tasks=tuple(ResidentExactPair.from_wire(item) for item in value["pairs"]),
            debug_delay_ms=value.get("debug_delay_ms", 0),
        )
        task.validate()
        return task

    @staticmethod
    def result_from_wire(value: Any) -> "ResidentExactBatchResult":
        return ResidentExactBatchResult.from_wire(value)


# Short name used by the process-path adapter and focused tests.
ResidentExactTask = ResidentExactBatchTask


@dataclass(frozen=True)
class BatchTask:
    identity: SnapshotIdentity
    batch_id: str
    pair_tasks: tuple[PairTask, ...]
    graphs: tuple[GraphData, ...]
    debug_delay_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("batch identity is invalid")
        _require_text(self.batch_id, "batch_id")
        if not self.pair_tasks:
            raise PayloadValidationError("batch must contain at least one pair")
        ordered = tuple(sorted(self.pair_tasks, key=lambda item: item.pair_ordinal))
        ordinals = tuple(item.pair_ordinal for item in ordered)
        if len(set(ordinals)) != len(ordinals):
            raise PayloadValidationError("batch contains duplicate pair ordinal")
        object.__setattr__(self, "pair_tasks", ordered)
        if isinstance(self.debug_delay_ms, bool) or not isinstance(self.debug_delay_ms, int) or not 0 <= self.debug_delay_ms <= 10000:
            raise PayloadValidationError("debug_delay_ms is out of bounds")

    @property
    def pair_ordinals(self) -> tuple[int, ...]:
        return tuple(item.pair_ordinal for item in self.pair_tasks)

    @property
    def graph_map(self) -> dict[str, GraphData]:
        return {item.graph_key: item for item in self.graphs}

    def validate(self) -> None:
        graph_map = self.graph_map
        if len(graph_map) != len(self.graphs):
            raise PayloadValidationError("batch contains duplicate graph key")
        for graph in self.graphs:
            graph.validate()
        for pair in self.pair_tasks:
            for reference in (pair.master_graph, pair.member_graph):
                graph = graph_map.get(reference.graph_key)
                if graph is None or graph.content_digest != reference.content_digest:
                    raise PayloadValidationError("pair graph reference mismatch")
        _canonical(self.to_wire())

    def to_wire(self) -> dict[str, Any]:
        self.validate_without_wire_recursion()
        return {
            "operation": "exact_correspondence_batch",
            "identity": self.identity.to_wire(),
            "batch_id": self.batch_id,
            "pairs": tuple(item.to_wire() for item in self.pair_tasks),
            "graphs": tuple(item.to_wire() for item in sorted(self.graphs, key=lambda item: item.graph_key)),
            "debug_delay_ms": self.debug_delay_ms,
        }

    def validate_without_wire_recursion(self) -> None:
        graph_map = self.graph_map
        if len(graph_map) != len(self.graphs):
            raise PayloadValidationError("batch contains duplicate graph key")
        for graph in self.graphs:
            graph.validate()
        for pair in self.pair_tasks:
            for reference in (pair.master_graph, pair.member_graph):
                graph = graph_map.get(reference.graph_key)
                if graph is None or graph.content_digest != reference.content_digest:
                    raise PayloadValidationError("pair graph reference mismatch")

    @classmethod
    def from_wire(cls, value: Any) -> "BatchTask":
        if not isinstance(value, Mapping) or value.get("operation") != "exact_correspondence_batch":
            raise PayloadValidationError("invalid exact batch operation")
        task = cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            batch_id=value["batch_id"],
            pair_tasks=tuple(PairTask.from_wire(item) for item in value["pairs"]),
            graphs=tuple(GraphData.from_wire(item) for item in value["graphs"]),
            debug_delay_ms=value.get("debug_delay_ms", 0),
        )
        task.validate()
        return task

    def payload_digest(self) -> str:
        return stable_digest(self.to_wire())

    def cache_key(self) -> "BatchCacheKey":
        pairs = tuple(
            (
                item.pair_ordinal,
                item.master_key,
                item.member_key,
                item.master_graph.to_wire(),
                item.member_graph.to_wire(),
                item.options.to_wire(),
                item.correspondence_mode,
            )
            for item in self.pair_tasks
        )
        return BatchCacheKey(
            schema_version=self.identity.schema_version,
            algorithm_version=self.identity.algorithm_version,
            generation=self.identity.generation,
            snapshot_digest=self.identity.snapshot_digest,
            pair_digest=stable_digest(pairs),
        )


@dataclass(frozen=True)
class TransformData:
    angle: float
    scale: float
    reflected: bool
    source_center: tuple[float, float]
    target_center: tuple[float, float]

    def __post_init__(self) -> None:
        _require_finite(self.angle, "transform angle")
        _require_finite(self.scale, "transform scale")
        _require_bool(self.reflected, "transform reflected")
        for name, center in (("source", self.source_center), ("target", self.target_center)):
            if not isinstance(center, (tuple, list)) or len(center) != 2:
                raise PayloadValidationError(f"transform {name} center must contain two values")
            for value in center:
                _require_finite(value, f"transform {name} center")

    def to_wire(self) -> tuple[Any, ...]:
        return (self.angle, self.scale, self.reflected, tuple(self.source_center), tuple(self.target_center))

    @classmethod
    def from_wire(cls, value: Any) -> Optional["TransformData"]:
        if value is None:
            return None
        if not isinstance(value, (tuple, list)) or len(value) != 5:
            raise PayloadValidationError("invalid transform wire value")
        return cls(value[0], value[1], value[2], tuple(value[3]), tuple(value[4]))


@dataclass(frozen=True)
class PairResult:
    pair_ordinal: int
    master_key: Any
    member_key: Any
    master_graph_digest: str
    member_graph_digest: str
    accepted: bool
    loop_mapping: tuple[tuple[Any, Any], ...] = ()
    reflected: bool = False
    reversed: bool = False
    cyclic_shift: int = 0
    score: Optional[float] = None
    residual: Optional[float] = None
    reason: str = ""
    transform: Optional[TransformData] = None
    diagnostics: tuple[tuple[str, Any], ...] = ()
    complete: bool = True
    correspondence_mode: str = CORRESPONDENCE_MODE_HYBRID

    def __post_init__(self) -> None:
        _require_uint(self.pair_ordinal, "pair_ordinal")
        _require_bool(self.accepted, "accepted")
        _require_bool(self.reflected, "reflected")
        _require_bool(self.reversed, "reversed")
        _require_bool(self.complete, "complete")
        object.__setattr__(
            self,
            "correspondence_mode",
            normalize_correspondence_mode(self.correspondence_mode),
        )
        _require_text(self.master_graph_digest, "master_graph_digest")
        _require_text(self.member_graph_digest, "member_graph_digest")
        _canonical(self.master_key)
        _canonical(self.member_key)
        normalised_diagnostics = []
        for item in self.diagnostics:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise PayloadValidationError("invalid pair result diagnostic item")
            name, value = str(item[0]), item[1]
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, int):
                value = int(value)
            elif isinstance(value, float):
                if not math.isfinite(value):
                    raise PayloadValidationError("pair result diagnostic is non-finite")
                value = float(value)
            else:
                raise PayloadValidationError("pair result diagnostic must be numeric")
            normalised_diagnostics.append((name, value))
        object.__setattr__(self, "diagnostics", tuple(normalised_diagnostics))
        normalised_mapping: list[tuple[Any, Any]] = []
        for item in self.loop_mapping:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise PayloadValidationError("loop mapping item must contain source and target keys")
            normalised_mapping.append((item[0], item[1]))
        mapping = tuple(sorted(normalised_mapping, key=lambda item: _stable_sort_key(item[0])))
        object.__setattr__(self, "loop_mapping", mapping)
        if self.score is not None:
            _require_finite(self.score, "score")
        if self.residual is not None:
            _require_finite(self.residual, "residual")
        _require_uint(self.cyclic_shift, "cyclic_shift")
        if not isinstance(self.complete, bool):
            raise PayloadValidationError("complete must be bool")

    @classmethod
    def from_correspondence(
        cls,
        pair: PairTask,
        result: Any,
        *,
        nearest_result: Any = None,
    ) -> "PairResult":
        transform = None
        if result.transform is not None:
            transform = TransformData(
                result.transform.angle,
                result.transform.scale,
                result.transform.reflected,
                tuple(result.transform.source_center),
                tuple(result.transform.target_center),
            )
        score = result.score if isinstance(result.score, (int, float)) and math.isfinite(float(result.score)) else None
        residual = result.residual if isinstance(result.residual, (int, float)) and math.isfinite(float(result.residual)) else None
        diagnostics = result.diagnostics
        diagnostic_fields = (
            "search_count", "complete_mappings", "pruned_count", "branch_budget",
            "candidate_count", "topology_checks", "yield_count",
            "refinement_rounds", "refinement_max_rounds", "refinement_stable",
            "refinement_truncated", "refinement_elapsed_us",
            "refinement_pre_max_domain", "refinement_post_max_domain",
        )
        diagnostics_wire = [
            (name, int(getattr(diagnostics, name)))
            for name in diagnostic_fields
        ]
        mode = normalize_correspondence_mode(pair.correspondence_mode)
        fast = nearest_result
        if fast is None and hasattr(result, "nearest_diagnostics"):
            fast = result
        fast_diagnostics = getattr(fast, "nearest_diagnostics", None)
        if fast_diagnostics is None and fast is not None:
            fast_diagnostics = getattr(fast, "diagnostics", None)
        if fast is not None and fast_diagnostics is not None:
            # A missing seed is explicitly a non-applicable fast path: the
            # worker skips nearest proof and enters the unchanged exact
            # fallback.  Count only a seed-bearing proof as attempted so the
            # report distinguishes that case from a verified-nearest miss.
            seed_supplied = bool(getattr(fast_diagnostics, "seed_supplied", True))
            fast_accepted = bool(getattr(fast, "accepted", False))
            nearest_attempted = int(
                seed_supplied
            )
            nearest_accepted = bool(
                nearest_attempted and fast_accepted
            )
            diagnostics_wire.extend(
                (
                    ("nearest_attempted", nearest_attempted),
                    ("nearest_accepted", int(nearest_accepted)),
                    (
                        "nearest_fallback",
                        int(nearest_attempted and not nearest_accepted),
                    ),
                    (
                        "nearest_max_seed_distance",
                        float(getattr(fast_diagnostics, "max_seed_distance", 0.0)),
                    ),
                    (
                        "nearest_mean_seed_distance",
                        float(getattr(fast_diagnostics, "mean_seed_distance", 0.0)),
                    ),
                    (
                        "nearest_ambiguity_count",
                        int(
                            getattr(
                                fast_diagnostics,
                                "ambiguity_count",
                                int(bool(getattr(fast_diagnostics, "ambiguous", False))),
                            )
                        ),
                    ),
                    (
                        "nearest_tie_count",
                        int(getattr(fast_diagnostics, "tie_count", 0)),
                    ),
                    (
                        "nearest_distance_evaluations",
                        int(
                            getattr(
                                fast_diagnostics,
                                "distance_evaluations",
                                getattr(fast_diagnostics, "nearest_distance_cache_evaluations", 0),
                            )
                        ),
                    ),
                    (
                        "nearest_assignment_nodes",
                        int(getattr(fast_diagnostics, "assignment_nodes", 0)),
                    ),
                    (
                        "nearest_assignment_cap",
                        int(getattr(fast_diagnostics, "assignment_cap", 0)),
                    ),
                    (
                        "nearest_distance_lookups",
                        int(getattr(fast_diagnostics, "distance_lookups", 0)),
                    ),
                    (
                        "nearest_distance_cache_hits",
                        int(getattr(fast_diagnostics, "distance_cache_hits", 0)),
                    ),
                    (
                        "nearest_distance_cache_misses",
                        int(getattr(fast_diagnostics, "distance_cache_misses", 0)),
                    ),
                    (
                        "nearest_operations_used",
                        int(getattr(fast_diagnostics, "operations_used", 0)),
                    ),
                    (
                        "nearest_fallback_reason_code",
                        int(getattr(fast_diagnostics, "fallback_reason_code", 0)),
                    ),
                    (
                        "nearest_compute_ms",
                        float(getattr(fast_diagnostics, "nearest_elapsed_us", 0)) / 1000.0,
                    ),
                    # These counters are typed evidence from the worker path.
                    # In particular, a missing seed is not a nearest attempt,
                    # while a seed-bearing fast miss is exactly one unchanged
                    # CorrespondenceSearch fallback.
                    ("graph_rejected_before_nearest", 0),
                    ("nearest_seed_missing", int(not seed_supplied)),
                    (
                        "nearest_fast_miss",
                        int(nearest_attempted and not fast_accepted),
                    ),
                    (
                        "exact_fallback_calls",
                        int(
                            mode == CORRESPONDENCE_MODE_HYBRID
                            and not fast_accepted
                        ),
                    ),
                    (
                        "exact_primary_calls",
                        int(mode == CORRESPONDENCE_MODE_EXACT_ONLY),
                    ),
                )
            )
        else:
            diagnostics_wire.extend(
                (
                    ("nearest_attempted", 0),
                    ("nearest_accepted", 0),
                    ("nearest_fallback", 0),
                    ("graph_rejected_before_nearest", 0),
                    ("nearest_seed_missing", 0),
                    ("nearest_fast_miss", 0),
                    ("exact_fallback_calls", 0),
                    (
                        "exact_primary_calls",
                        int(mode == CORRESPONDENCE_MODE_EXACT_ONLY),
                    ),
                )
            )
        reason = str(result.reason)
        if (
            mode == CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
            and not bool(result.accepted)
        ):
            fallback_reason = str(
                getattr(result, "fallback_reason", "")
                or getattr(fast_diagnostics, "fallback_reason", "")
                or reason
                or "fallback_required"
            )
            reason = "fast_unverified:%s" % fallback_reason
        return cls(
            pair_ordinal=pair.pair_ordinal,
            master_key=pair.master_key,
            member_key=pair.member_key,
            master_graph_digest=pair.master_graph.content_digest,
            member_graph_digest=pair.member_graph.content_digest,
            accepted=bool(result.accepted),
            loop_mapping=tuple(result.loop_mapping),
            reflected=bool(result.reflected),
            reversed=bool(result.reversed),
            cyclic_shift=int(result.cyclic_shift),
            score=score,
            residual=residual,
            reason=reason,
            transform=transform,
            diagnostics=tuple(diagnostics_wire),
            correspondence_mode=mode,
        )

    def validate(self, pair: PairTask, graph_map: Mapping[str, GraphData]) -> None:
        if not self.complete:
            raise PayloadValidationError("incomplete pair result is not consumable")
        if self.pair_ordinal != pair.pair_ordinal or self.master_key != pair.master_key or self.member_key != pair.member_key:
            raise PayloadValidationError("pair result identity mismatch")
        if self.master_graph_digest != pair.master_graph.content_digest or self.member_graph_digest != pair.member_graph.content_digest:
            raise PayloadValidationError("pair result graph digest mismatch")
        if self.correspondence_mode != pair.correspondence_mode:
            raise PayloadValidationError("pair result correspondence mode mismatch")
        master = graph_map[pair.master_graph.graph_key]
        member = graph_map[pair.member_graph.graph_key]
        if self.accepted:
            source = {item.key for item in member.loops}
            target = {item.key for item in master.loops}
            mapped_source = {item[0] for item in self.loop_mapping}
            mapped_target = {item[1] for item in self.loop_mapping}
            if len(self.loop_mapping) != len(source) or len(mapped_source) != len(self.loop_mapping) or len(mapped_target) != len(self.loop_mapping):
                raise PayloadValidationError("accepted mapping is not a full bijection")
            if mapped_source != source or mapped_target != target:
                raise PayloadValidationError("accepted mapping misses loop keys")
            if self.score is None or self.residual is None or self.transform is None:
                raise PayloadValidationError("accepted result lacks finite metrics/transform")
        elif self.loop_mapping:
            raise PayloadValidationError("rejected result must have empty mapping")
        _canonical(self.to_wire())

    def to_wire(self) -> tuple[Any, ...]:
        return (
            "pair-result",
            self.pair_ordinal,
            self.master_key,
            self.member_key,
            self.master_graph_digest,
            self.member_graph_digest,
            self.accepted,
            tuple(self.loop_mapping),
            self.reflected,
            self.reversed,
            self.cyclic_shift,
            self.score,
            self.residual,
            self.reason,
            None if self.transform is None else self.transform.to_wire(),
            tuple(self.diagnostics),
            self.complete,
            self.correspondence_mode,
        )

    @classmethod
    def from_wire(cls, value: Any) -> "PairResult":
        if not isinstance(value, (tuple, list)) or len(value) not in (17, 18) or value[0] != "pair-result":
            raise PayloadValidationError("invalid pair result wire value")
        raw_mapping = value[7]
        if not isinstance(raw_mapping, (tuple, list)):
            raise PayloadValidationError("invalid pair result mapping wire value")
        mapping: list[tuple[Any, Any]] = []
        for item in raw_mapping:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise PayloadValidationError("invalid pair result mapping item")
            mapping.append((item[0], item[1]))
        raw_diagnostics = value[15]
        if not isinstance(raw_diagnostics, (tuple, list)):
            raise PayloadValidationError("invalid pair result diagnostics wire value")
        diagnostics: list[tuple[str, Any]] = []
        for item in raw_diagnostics:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise PayloadValidationError("invalid pair result diagnostic item")
            raw_value = item[1]
            if isinstance(raw_value, bool):
                raw_value = int(raw_value)
            elif isinstance(raw_value, int):
                raw_value = int(raw_value)
            elif isinstance(raw_value, float) and math.isfinite(raw_value):
                raw_value = float(raw_value)
            else:
                raise PayloadValidationError("invalid pair result diagnostic value")
            diagnostics.append((str(item[0]), raw_value))
        return cls(
            pair_ordinal=value[1], master_key=value[2], member_key=value[3],
            master_graph_digest=value[4], member_graph_digest=value[5], accepted=value[6],
            loop_mapping=tuple(mapping), reflected=value[8],
            reversed=value[9], cyclic_shift=value[10], score=value[11], residual=value[12],
            reason=value[13], transform=TransformData.from_wire(value[14]),
            diagnostics=tuple(diagnostics), complete=value[16],
            correspondence_mode=(
                CORRESPONDENCE_MODE_HYBRID if len(value) == 17 else value[17]
            ),
        )


def _pair_result_digest_wire(result: Any) -> Any:
    """Return semantic result wire data without nondeterministic wall time."""

    value = result.to_wire()
    if not isinstance(value, tuple) or len(value) < 17 or value[0] != "pair-result":
        return value
    diagnostics = tuple(
        item
        for item in value[15]
        if item[0] not in {"refinement_elapsed_us", "nearest_compute_ms"}
    )
    return tuple(value[:15]) + (diagnostics, value[16]) + tuple(value[17:])


def pair_result_digest_wire(result: Any) -> Any:
    """Public semantic-wire helper for pool/pipeline aggregate digests."""

    return _pair_result_digest_wire(result)


@dataclass(frozen=True)
class ResidentExactBatchResult:
    """Complete resident exact outcomes; no GraphData crosses the wire."""

    identity: SnapshotIdentity
    context_digest: str
    batch_id: str
    payload_digest: str
    pair_results: tuple[PairResult, ...]
    complete: bool = True
    graph_cache_builds: int = 0
    graph_cache_hits: int = 0
    graph_compute_ms: float = 0.0
    exact_compute_ms: float = 0.0
    topology_cache_builds: int = 0
    topology_cache_hits: int = 0
    topology_compute_ms: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("resident exact result identity is invalid")
        _require_text(self.context_digest, "resident exact result context digest")
        _require_text(self.batch_id, "resident exact result batch_id")
        _require_text(self.payload_digest, "resident exact result payload digest")
        _require_bool(self.complete, "resident exact result complete")
        for name, value in (
            ("graph_cache_builds", self.graph_cache_builds),
            ("graph_cache_hits", self.graph_cache_hits),
        ):
            _require_uint(value, name)
        _require_finite(self.graph_compute_ms, "resident graph compute_ms")
        _require_finite(self.exact_compute_ms, "resident exact compute_ms")
        for name, value in (
            ("topology_cache_builds", self.topology_cache_builds),
            ("topology_cache_hits", self.topology_cache_hits),
        ):
            _require_uint(value, name)
        _require_finite(self.topology_compute_ms, "resident topology compute_ms")
        ordered = tuple(sorted(self.pair_results, key=lambda item: item.pair_ordinal))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PayloadValidationError("resident exact result contains duplicate ordinal")
        if any(not isinstance(item, PairResult) for item in ordered):
            raise PayloadValidationError("resident exact result contains an invalid pair")
        object.__setattr__(self, "pair_results", ordered)

    @staticmethod
    def _validate_pair(pair: ResidentExactPair, result: PairResult) -> None:
        if not result.complete:
            raise PayloadValidationError("incomplete resident pair result is not consumable")
        if (
            result.pair_ordinal != pair.pair_ordinal
            or result.master_key != pair.master_key
            or result.member_key != pair.member_key
        ):
            raise PayloadValidationError("resident pair result identity mismatch")
        if not result.master_graph_digest or not result.member_graph_digest:
            raise PayloadValidationError("resident pair result lacks graph identity")
        if result.correspondence_mode != pair.correspondence_mode:
            raise PayloadValidationError("resident pair result correspondence mode mismatch")
        source = set(pair.member_loop_keys)
        target = set(pair.master_loop_keys)
        if result.accepted:
            mapped_source = {item[0] for item in result.loop_mapping}
            mapped_target = {item[1] for item in result.loop_mapping}
            if (
                len(result.loop_mapping) != len(source)
                or len(mapped_source) != len(result.loop_mapping)
                or len(mapped_target) != len(result.loop_mapping)
                or mapped_source != source
                or mapped_target != target
            ):
                raise PayloadValidationError("resident accepted mapping is not a full bijection")
            if result.score is None or result.residual is None or result.transform is None:
                raise PayloadValidationError("resident accepted result lacks finite metrics/transform")
        elif result.loop_mapping:
            raise PayloadValidationError("resident rejected result must have empty mapping")

    def validate_against(self, task: ResidentExactBatchTask) -> None:
        task.validate()
        if not self.complete:
            raise PayloadValidationError("incomplete resident exact result is not consumable")
        if (
            self.identity != task.identity
            or self.context_digest != task.context_digest
            or self.batch_id != task.batch_id
            or self.payload_digest != task.payload_digest()
        ):
            raise PayloadValidationError("resident exact result identity/digest mismatch")
        if tuple(item.pair_ordinal for item in self.pair_results) != task.pair_ordinals:
            raise PayloadValidationError("resident exact result ordinal coverage mismatch")
        pair_map = {item.pair_ordinal: item for item in task.pair_tasks}
        for result in self.pair_results:
            pair = pair_map.get(result.pair_ordinal)
            if pair is None:
                raise PayloadValidationError("resident exact result has foreign ordinal")
            self._validate_pair(pair, result)

    def result_digest(self) -> str:
        return stable_digest(
            (
                self.identity.to_wire(),
                self.context_digest,
                self.batch_id,
                self.payload_digest,
                tuple(_pair_result_digest_wire(item) for item in self.pair_results),
            )
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": RESIDENT_EXACT_RESULT_OPERATION,
            "identity": self.identity.to_wire(),
            "context_digest": self.context_digest,
            "batch_id": self.batch_id,
            "payload_digest": self.payload_digest,
            "pair_results": tuple(item.to_wire() for item in self.pair_results),
            "complete": self.complete,
            "graph_cache_builds": self.graph_cache_builds,
            "graph_cache_hits": self.graph_cache_hits,
            "graph_compute_ms": self.graph_compute_ms,
            "exact_compute_ms": self.exact_compute_ms,
            "topology_cache_builds": self.topology_cache_builds,
            "topology_cache_hits": self.topology_cache_hits,
            "topology_compute_ms": self.topology_compute_ms,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "ResidentExactBatchResult":
        if not isinstance(value, Mapping) or value.get("operation") != RESIDENT_EXACT_RESULT_OPERATION:
            raise PayloadValidationError("invalid resident exact result operation")
        return cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            context_digest=value["context_digest"],
            batch_id=value["batch_id"],
            payload_digest=value["payload_digest"],
            pair_results=tuple(PairResult.from_wire(item) for item in value["pair_results"]),
            complete=value.get("complete", False),
            graph_cache_builds=value.get("graph_cache_builds", 0),
            graph_cache_hits=value.get("graph_cache_hits", 0),
            graph_compute_ms=value.get("graph_compute_ms", 0.0),
            exact_compute_ms=value.get("exact_compute_ms", 0.0),
            topology_cache_builds=value.get("topology_cache_builds", 0),
            topology_cache_hits=value.get("topology_cache_hits", 0),
            topology_compute_ms=value.get("topology_compute_ms", 0.0),
        )


@dataclass(frozen=True)
class BatchResult:
    identity: SnapshotIdentity
    batch_id: str
    payload_digest: str
    pair_results: tuple[PairResult, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SnapshotIdentity):
            raise PayloadValidationError("batch result identity is invalid")
        _require_text(self.batch_id, "batch_id")
        _require_text(self.payload_digest, "payload_digest")
        _require_bool(self.complete, "complete")
        ordered = tuple(sorted(self.pair_results, key=lambda item: item.pair_ordinal))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PayloadValidationError("batch result contains duplicate ordinal")
        object.__setattr__(self, "pair_results", ordered)

    def validate_against(self, task: BatchTask) -> None:
        task.validate()
        if not self.complete:
            raise PayloadValidationError("incomplete batch result is not consumable")
        if self.identity != task.identity or self.batch_id != task.batch_id:
            raise PayloadValidationError("batch result identity mismatch")
        if self.payload_digest != task.payload_digest():
            raise PayloadValidationError("batch result payload digest mismatch")
        pair_map = {item.pair_ordinal: item for item in task.pair_tasks}
        if tuple(item.pair_ordinal for item in self.pair_results) != task.pair_ordinals:
            raise PayloadValidationError("batch result ordinal coverage mismatch")
        graph_map = task.graph_map
        for result in self.pair_results:
            pair = pair_map.get(result.pair_ordinal)
            if pair is None:
                raise PayloadValidationError("batch result has foreign ordinal")
            result.validate(pair, graph_map)

    def result_digest(self) -> str:
        return stable_digest(
            (
                self.identity.to_wire(),
                self.batch_id,
                self.payload_digest,
                tuple(_pair_result_digest_wire(item) for item in self.pair_results),
            )
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "operation": "exact_correspondence_result",
            "identity": self.identity.to_wire(),
            "batch_id": self.batch_id,
            "payload_digest": self.payload_digest,
            "pair_results": tuple(item.to_wire() for item in self.pair_results),
            "complete": self.complete,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "BatchResult":
        if not isinstance(value, Mapping) or value.get("operation") != "exact_correspondence_result":
            raise PayloadValidationError("invalid exact result operation")
        return cls(
            identity=SnapshotIdentity.from_wire(value["identity"]),
            batch_id=value["batch_id"],
            payload_digest=value["payload_digest"],
            pair_results=tuple(PairResult.from_wire(item) for item in value["pair_results"]),
            complete=value.get("complete", False),
        )


@dataclass(frozen=True)
class BatchCacheKey:
    schema_version: str
    algorithm_version: str
    generation: int
    snapshot_digest: str
    pair_digest: str


class CompleteResultCache:
    """Cache that accepts only validated, complete batch results."""

    def __init__(self) -> None:
        self._values: dict[BatchCacheKey, BatchResult] = {}

    def get(self, key: BatchCacheKey) -> Optional[BatchResult]:
        return self._values.get(key)

    def put(self, task: BatchTask, result: BatchResult) -> None:
        result.validate_against(task)
        if not result.complete:
            raise PayloadValidationError("partial result cannot enter cache")
        self._values[task.cache_key()] = result

    def clear(self) -> None:
        self._values.clear()

    def clear_generation(self, generation: int) -> None:
        self._values = {
            key: value for key, value in self._values.items() if key.generation == generation
        }

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True)
class SerializationEstimate:
    payload_bytes: int
    frame_bytes: int
    payload_digest: str
    protocol: int = PICKLE_PROTOCOL


def serialize_batch_task(task: BatchTask) -> bytes:
    """Serialize one validated task with the fixed protocol-5 wire payload."""

    task.validate()
    payload_bytes = pickle.dumps(task.to_wire(), protocol=PICKLE_PROTOCOL)
    if len(payload_bytes) > MAX_FRAME_BYTES:
        raise FrameSizeError("serialized batch payload exceeds MAX_FRAME_BYTES")
    nonce_bytes = task.identity.session_nonce.encode("utf-8")
    batch_bytes = task.batch_id.encode("utf-8")
    frame_bytes = FRAME_PREFIX_BYTES + FRAME_HEADER_BYTES + len(nonce_bytes) + len(batch_bytes) + len(payload_bytes)
    if frame_bytes > MAX_FRAME_BYTES:
        raise FrameSizeError("serialized batch frame exceeds MAX_FRAME_BYTES")
    return payload_bytes


def estimate_batch_frame(task: BatchTask) -> SerializationEstimate:
    payload_bytes = serialize_batch_task(task)
    nonce_bytes = task.identity.session_nonce.encode("utf-8")
    batch_bytes = task.batch_id.encode("utf-8")
    frame_bytes = FRAME_PREFIX_BYTES + FRAME_HEADER_BYTES + len(nonce_bytes) + len(batch_bytes) + len(payload_bytes)
    return SerializationEstimate(
        payload_bytes=len(payload_bytes),
        frame_bytes=frame_bytes,
        payload_digest=hashlib.sha256(payload_bytes).hexdigest().upper(),
    )


measure_batch_serialization = estimate_batch_frame


def partition_batches(
    identity: SnapshotIdentity,
    pair_tasks: Iterable[PairTask],
    graphs: Iterable[GraphData],
    *,
    batch_size: int = 64,
) -> tuple[BatchTask, ...]:
    """Pack canonical pairs by master affinity without losing ordinals."""

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 96:
        raise PayloadValidationError("batch_size must be between 1 and 96")
    pairs = tuple(sorted(tuple(pair_tasks), key=lambda item: item.pair_ordinal))
    if not pairs:
        return ()
    ordinals = tuple(item.pair_ordinal for item in pairs)
    if len(set(ordinals)) != len(ordinals):
        raise PayloadValidationError("pair planner contains duplicate ordinal")
    graph_values = tuple(graphs)
    graph_map: dict[str, GraphData] = {}
    for graph in graph_values:
        previous = graph_map.get(graph.graph_key)
        if previous is not None and previous.content_digest != graph.content_digest:
            raise PayloadValidationError("planner contains conflicting graph key")
        graph_map[graph.graph_key] = graph
    for graph in graph_map.values():
        graph.validate()
    # Keep only contiguous master runs.  A global regrouping would improve
    # cache affinity but would silently reorder an interleaved canonical plan.
    # The planner's ordinal order is the stronger invariant; contiguous runs
    # still keep the common master-affine case together.
    groups: list[list[PairTask]] = []
    current_group: list[PairTask] = []
    current_key: Optional[str] = None
    for pair in pairs:
        group_key = _stable_sort_key(pair.master_key)
        if current_group and group_key != current_key:
            groups.append(current_group)
            current_group = []
        current_group.append(pair)
        current_key = group_key
    if current_group:
        groups.append(current_group)
    grouped = groups
    chunks: list[tuple[PairTask, ...]] = []
    for group in grouped:
        for offset in range(0, len(group), batch_size):
            part = group[offset : offset + batch_size]
            # Do not merge two master runs merely to fill a batch. Keeping a
            # batch master-affine makes worker graph-cache reuse predictable;
            # the canonical ordinal sequence remains the final guard below.
            chunks.append(tuple(sorted(part, key=lambda item: item.pair_ordinal)))

    result: list[BatchTask] = []
    for index, chunk in enumerate(chunks):
        needed: list[GraphData] = []
        seen: set[str] = set()
        for pair in chunk:
            for reference in (pair.master_graph, pair.member_graph):
                if reference.graph_key not in seen:
                    graph = graph_map.get(reference.graph_key)
                    if graph is None or graph.content_digest != reference.content_digest:
                        raise PayloadValidationError("partition graph reference mismatch")
                    seen.add(reference.graph_key)
                    needed.append(graph)
        task = BatchTask(identity, f"batch-{index:06d}", chunk, tuple(needed))
        task.validate()
        estimate_batch_frame(task)
        result.append(task)
    flattened = tuple(item.pair_ordinal for task in result for item in task.pair_tasks)
    if flattened != ordinals:
        raise PayloadValidationError("partition changed canonical ordinal sequence")
    return tuple(result)


__all__ = [
    "SCHEMA_VERSION", "ALGORITHM_VERSION", "PICKLE_PROTOCOL", "MAX_FRAME_BYTES",
    "graph_context_material_digest", "graph_context_identity_digest",
    "PRODUCTION_BATCH_SIZES", "PayloadValidationError", "FrameSizeError", "stable_digest",
    "CORRESPONDENCE_MODE_HYBRID", "CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY",
    "CORRESPONDENCE_MODE_EXACT_ONLY", "CORRESPONDENCE_MODES",
    "normalize_correspondence_mode",
    "SnapshotIdentity", "ExactOptions", "GraphRef", "GraphLoopData", "GraphFaceData",
    "GraphEdgeData", "GraphVertexData", "GraphBoundaryData", "GraphData",
    "GRAPH_BUILD_OPERATION", "GRAPH_BUILD_RESULT_OPERATION", "GRAPH_CONTEXT_OPERATION",
    "GRAPH_CONTEXT_ACK_OPERATION", "RESIDENT_EXACT_OPERATION",
    "RESIDENT_EXACT_RESULT_OPERATION", "FUSED_CONTEXT_VERSION",
    "graph_context_item_digest", "fused_context_digest", "GraphContextPayload",
    "GraphContextLoadTask", "GraphContextLoadResult", "GraphBuildItem", "GraphBuildTask",
    "GraphBuildBatch", "GraphBuildEntry", "GraphBuildResult",
    "PairTask", "ResidentExactPair", "ResidentExactBatchTask", "ResidentExactTask",
    "ResidentExactBatchResult", "BatchTask", "TransformData", "PairResult", "BatchResult", "BatchCacheKey",
    "CompleteResultCache", "SerializationEstimate", "serialize_batch_task",
    "pair_result_digest_wire",
    "estimate_batch_frame", "measure_batch_serialization", "partition_batches",
]
