"""Pure adapter between the live Pro session and MC2 wire records.

The adapter is deliberately the only MC3A boundary that understands both the
immutable topology records and the small amount of Blender-owned state needed
to establish a snapshot identity.  It never imports :mod:`bpy` and it never
keeps a BMesh, loop, or other mutable Blender object in a payload.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import math
from pathlib import Path
import pickle
import sys
import time
import traceback
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

try:
    from .pro_process_payload import (
        BatchTask,
        CORRESPONDENCE_MODE_HYBRID,
        ExactOptions,
        GraphData,
        GraphBoundaryData,
        GraphBuildItem,
        GraphBuildTask,
        GraphContextPayload,
        fused_context_digest,
        graph_context_item_digest,
        graph_context_identity_digest,
        graph_context_material_digest,
        GraphEdgeData,
        GraphFaceData,
        GraphLoopData,
        GraphVertexData,
        GraphRef,
        PairResult,
        PairTask,
        ResidentExactBatchTask,
        ResidentExactPair,
        SnapshotIdentity,
        normalize_correspondence_mode,
        _canonical as _payload_canonical,
        stable_digest,
    )
    from .pro_process_shape import (
        FusedBatchTask,
        FusedPairRef,
        ShapeDescriptor,
        ShapeOptions,
    )
except ImportError:  # direct-file unit loading without package initialisation
    from pro_process_payload import (  # type: ignore[no-redef]
        BatchTask,
        CORRESPONDENCE_MODE_HYBRID,
        ExactOptions,
        GraphData,
        GraphBoundaryData,
        GraphBuildItem,
        GraphBuildTask,
        GraphContextPayload,
        fused_context_digest,
        graph_context_item_digest,
        graph_context_identity_digest,
        graph_context_material_digest,
        GraphEdgeData,
        GraphFaceData,
        GraphLoopData,
        GraphVertexData,
        GraphRef,
        PairResult,
        PairTask,
        ResidentExactBatchTask,
        ResidentExactPair,
        SnapshotIdentity,
        normalize_correspondence_mode,
        _canonical as _payload_canonical,
        stable_digest,
    )
    try:
        from pro_process_shape import (  # type: ignore[no-redef]
            FusedBatchTask,
            FusedPairRef,
            ShapeDescriptor,
            ShapeOptions,
        )
    except ImportError:
        _shape_path = Path(__file__).with_name("pro_process_shape.py")
        _shape_spec = importlib.util.spec_from_file_location(
            "pro_process_shape", _shape_path
        )
        if _shape_spec is None or _shape_spec.loader is None:
            raise ProcessAdapterError("cannot load pure fused shape schema")
        _shape_module = importlib.util.module_from_spec(_shape_spec)
        sys.modules.setdefault("pro_process_shape", _shape_module)
        _shape_spec.loader.exec_module(_shape_module)
        FusedBatchTask = _shape_module.FusedBatchTask
        FusedPairRef = _shape_module.FusedPairRef
        ShapeDescriptor = _shape_module.ShapeDescriptor
        ShapeOptions = _shape_module.ShapeOptions


class ProcessAdapterError(ValueError):
    """A live object cannot be represented by the immutable process schema."""


_DIAGNOSTIC_REPR_LIMIT = 192


def _legacy_uv_token(uv: Any) -> tuple[float, float]:
    """Match the established BMesh boundary endpoint token exactly."""

    return (round(float(uv[0]), 10), round(float(uv[1]), 10))


def _graph_content_digest(
    graph_key: str,
    faces: tuple[GraphFaceData, ...],
    edges: tuple[GraphEdgeData, ...],
    vertices: tuple[GraphVertexData, ...],
    loops: tuple[GraphLoopData, ...],
    boundaries: tuple[GraphBoundaryData, ...],
    canonical_parts: Optional[tuple[tuple[Any, ...], ...]] = None,
) -> str:
    """Build the GraphData digest from already canonicalized record parts.

    ``GraphData.__post_init__`` normally canonicalizes the complete graph in
    one call.  SnapshotGraphBuilder has already canonicalized each immutable
    record as it was produced, so only the small top-level pickle remains in
    the final primitive.  The resulting digest is byte-identical to
    ``GraphData.computed_content_digest``.
    """

    if canonical_parts is None:
        canonical_parts = (
            tuple(_payload_canonical(item.to_wire()) for item in faces),
            tuple(_payload_canonical(item.to_wire()) for item in edges),
            tuple(_payload_canonical(item.to_wire()) for item in vertices),
            tuple(_payload_canonical(item.to_wire()) for item in loops),
            tuple(_payload_canonical(item.to_wire()) for item in boundaries),
        )
    canonical_content = (
        "seq",
        (
            _payload_canonical(graph_key),
            ("seq", canonical_parts[0]),
            ("seq", canonical_parts[1]),
            ("seq", canonical_parts[2]),
            ("seq", canonical_parts[3]),
            ("seq", canonical_parts[4]),
        ),
    )
    encoded = pickle.dumps(canonical_content, protocol=5)
    return hashlib.sha256(encoded).hexdigest().upper()


def _safe_diagnostic_repr(value: Any) -> str:
    """Keep probe diagnostics bounded and free of user payload dumps."""

    try:
        rendered = repr(value)
    except Exception as exc:  # pragma: no cover - defensive Blender wrapper
        rendered = "<repr-error:%s>" % type(exc).__name__
    return str(rendered)[:_DIAGNOSTIC_REPR_LIMIT]


def _diagnostic_value(label: str, value: Any) -> dict[str, str]:
    return {
        "label": str(label),
        "type": type(value).__name__,
        "repr": _safe_diagnostic_repr(value),
    }


@dataclass(frozen=True)
class SnapshotCapture:
    """The immutable identity and canonical primitive material used to make it."""

    identity: SnapshotIdentity
    canonical: tuple[Any, ...]
    material: Any = None


@dataclass(frozen=True)
class SnapshotMaterial:
    """Primitive topology captured once for the live process session.

    The mapping fields are read-only views.  They deliberately contain only
    tuples and scalar values; the live BMesh loop map remains a main-thread
    adapter concern and is never put in a worker task.
    """

    mesh: tuple[Any, ...]
    face_payload: tuple[Any, ...]
    edge_payload: tuple[Any, ...]
    vertex_payload: tuple[Any, ...]
    loop_payload: tuple[Any, ...]
    island_payload: tuple[Any, ...]
    island_face_keys: tuple[tuple[tuple[int, ...], tuple[Any, ...]], ...]
    face_by_key: Mapping[Any, Any]
    edge_by_key: Mapping[Any, Any]
    vertex_by_key: Mapping[Any, Any]
    loop_by_key: Mapping[Any, Any]
    # Pre-indexed immutable adjacency for bounded per-island projection.  The
    # maps are built once when the initial snapshot is finalized; graph task
    # creation must not rescan the complete mesh for every island.
    edge_items_by_loop: Mapping[Any, tuple[Any, ...]] = ()
    vertex_items_by_loop: Mapping[Any, tuple[Any, ...]] = ()


@dataclass(frozen=True)
class SnapshotSentinel:
    """Cheap live-context identity checked between immutable work slices."""

    object_identity: int
    data_identity: int
    bmesh_identity: int
    uv_layer_identity: int
    object_name: Any
    data_name: Any
    mode: Any
    uv_layer_name: Any
    active_face: Any
    selected_objects: tuple[str, ...]
    face_count: int
    edge_count: int
    vertex_count: int
    # A bounded UV sample catches an immediate edit without scanning the
    # topology.  The full immutable digest remains the authoritative guard
    # for unsampled UV changes before atomic apply.
    uv_samples: tuple[Any, ...]


def _cheap_uv_samples(bm: Any, uv_layer: Any) -> tuple[Any, ...]:
    """Return a deterministic O(1) UV sentinel from mesh endpoints."""

    if bm is None or uv_layer is None:
        return ()
    faces = getattr(bm, "faces", ()) or ()
    try:
        count = len(faces)
    except (TypeError, AttributeError):
        return ()
    if count <= 0:
        return ()
    indexes = (0,) if count == 1 else (0, count - 1)
    samples = []
    for face_index in indexes:
        try:
            face = faces[face_index]
            loops = tuple(getattr(face, "loops", ()) or ())
        except (IndexError, TypeError, AttributeError):
            continue
        for local_index in (0, len(loops) - 1):
            if not loops or local_index < 0:
                continue
            try:
                loop = loops[local_index]
                samples.append((
                    (_index(face), int(local_index)),
                    _uv_pair(loop, uv_layer),
                ))
            except (IndexError, KeyError, TypeError, AttributeError, ValueError):
                continue
    return tuple(sorted(set(samples), key=repr))


def make_snapshot_sentinel(context: Any, obj: Any, bm: Any, uv_layer: Any) -> SnapshotSentinel:
    """Read only O(1)-ish context state; never scans UV/topology payload."""

    faces = getattr(bm, "faces", ()) if bm is not None else ()
    active_face = getattr(faces, "active", None)
    data = getattr(obj, "data", None)
    uv_layers = getattr(data, "uv_layers", None)
    active_uv = getattr(uv_layers, "active", None)
    return SnapshotSentinel(
        object_identity=id(obj),
        data_identity=id(data),
        bmesh_identity=id(bm),
        uv_layer_identity=id(uv_layer),
        object_name=getattr(obj, "name", None),
        data_name=getattr(data, "name", None),
        mode=getattr(obj, "mode", None),
        uv_layer_name=getattr(active_uv, "name", getattr(uv_layer, "name", None)),
        active_face=None if active_face is None else _index(active_face),
        selected_objects=_selected_object_names(context),
        face_count=len(faces) if faces is not None else 0,
        edge_count=len(getattr(bm, "edges", ()) or ()) if bm is not None else 0,
        vertex_count=len(getattr(bm, "verts", ()) or ()) if bm is not None else 0,
        uv_samples=_cheap_uv_samples(bm, uv_layer),
    )


class IncrementalSnapshotBuilder:
    """Build the process snapshot without a monolithic BMesh scan.

    Each loop/edge/vertex/island is one resumable unit.  The resulting digest
    is intentionally versioned separately from the legacy ``capture_snapshot``
    digest: the process path validates against this same incremental framing,
    while the legacy API remains byte-compatible for MC3A tests.
    """

    def __init__(
        self,
        context: Any,
        obj: Any,
        bm: Any,
        uv_layer: Any,
        islands: Optional[Iterable[Iterable[Any]]],
        *,
        session_nonce: str,
        generation: int,
        options: Any,
    ) -> None:
        self.context = context
        self.obj = obj
        self.bm = bm
        self.uv_layer = uv_layer
        self.session_nonce = str(session_nonce)
        self.generation = int(generation)
        self.options = options
        self._face_iter = iter(getattr(bm, "faces", ()) or ())
        self._island_iter = iter(islands or ())
        self._faces_source: list[Any] = []
        self._faces: tuple[Any, ...] = ()
        self._face_index = 0
        self._current_face: Any = None
        self._current_face_loops: Any = None
        self._current_loop_index = 0
        self._face_loop_keys: dict[int, tuple[Any, ...]] = {}
        self._loop_payload: list[Any] = []
        self._loop_payload_by_key: dict[Any, tuple[Any, ...]] = {}
        self._loop_objects: dict[Any, Any] = {}
        self._loop_key_by_id: dict[int, Any] = {}
        self._edge_loops: dict[int, list[Any]] = {}
        self._edge_objects: dict[int, Any] = {}
        self._vertex_loops: dict[int, list[Any]] = {}
        self._vertex_objects: dict[int, Any] = {}
        self._vertex_uvs: dict[int, set[tuple[float, float]]] = {}
        self._face_payload: list[Any] = []
        self._edge_payload: list[Any] = []
        self._edge_boundary_by_key: dict[int, bool] = {}
        self._vertex_payload: list[Any] = []
        self._island_payload: list[Any] = []
        self._island_face_keys: list[tuple[tuple[int, ...], tuple[Any, ...]]] = []
        self._uv_split: dict[int, bool] = {}
        self._edge_keys: tuple[int, ...] = ()
        self._edge_index = 0
        self._vertex_keys: tuple[int, ...] = ()
        self._vertex_index = 0
        self._loop_sorted: tuple[Any, ...] = ()
        self._loop_index = 0
        self._islands_done = False
        self._phase = "faces"
        self._result: Optional[SnapshotCapture] = None
        self._finalize_state = "prepare"
        self._finalize_index = 0
        self._finalize_mesh: Any = None
        self._finalize_canonical: Any = None
        self._finalize_face_by_key: dict[Any, Any] = {}
        self._finalize_edge_by_key: dict[Any, Any] = {}
        self._finalize_vertex_by_key: dict[Any, Any] = {}
        self._finalize_loop_by_key: dict[Any, Any] = {}
        self._finalize_edge_items_by_loop: dict[Any, tuple[Any, ...]] = {}
        self._finalize_vertex_items_by_loop: dict[Any, tuple[Any, ...]] = {}
        self._finalize_face_payload: tuple[Any, ...] = ()
        self._finalize_edge_payload: tuple[Any, ...] = ()
        self._finalize_vertex_payload: tuple[Any, ...] = ()
        self._finalize_loop_payload: tuple[Any, ...] = ()
        self._finalize_island_payload: tuple[Any, ...] = ()
        self._finalize_island_face_keys: tuple[Any, ...] = ()
        self.current_primitive: dict[str, Any] = {
            "kind": "header",
            "phase": "faces",
            "index": 0,
            "key": None,
        }
        self._last_observed: Optional[dict[str, str]] = None
        self.failure_diagnostics: Optional[dict[str, Any]] = None
        self._live_loop_map: dict[Any, Any] = {}
        self._prewrite_snapshot: dict[Any, tuple[float, float]] = {}
        self._selection_snapshot: dict[Any, tuple[bool, ...]] = {}
        self.slices = 0
        self.primitive_operations = 0
        self.max_slice_ms = 0.0
        self.elapsed_ms = 0.0
        self.max_primitive_ms = 0.0
        self.max_primitive: dict[str, Any] = {}
        self.last_primitive: dict[str, Any] = {}
        self.phase_transitions: list[tuple[str, str]] = []
        self._digest = hashlib.sha256()
        try:
            self._digest_update(
                (
                    "mc4c2-header",
                    self.generation,
                    getattr(obj, "name", None),
                    getattr(getattr(obj, "data", None), "name", None),
                    getattr(uv_layer, "name", None),
                    _active_state(context, obj, bm),
                    options.to_wire() if hasattr(options, "to_wire") else options,
                )
            )
        except Exception as exc:
            self._capture_failure(exc, 0)
            raise

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def result(self) -> Optional[SnapshotCapture]:
        return self._result

    @property
    def live_loop_map(self) -> Mapping[Any, Any]:
        return MappingProxyType(dict(self._live_loop_map))

    @property
    def prewrite_snapshot(self) -> Mapping[Any, tuple[float, float]]:
        return MappingProxyType(dict(self._prewrite_snapshot))

    @property
    def selection_snapshot(self) -> Mapping[Any, tuple[bool, ...]]:
        return MappingProxyType(dict(self._selection_snapshot))

    def _set_phase(self, phase: str) -> None:
        if self._phase != phase:
            self.phase_transitions.append((self._phase, phase))
            self._phase = phase

    def _digest_update(self, value: Any) -> None:
        payload = pickle.dumps(value, protocol=5)
        self._digest.update(len(payload).to_bytes(8, "little", signed=False))
        self._digest.update(payload)

    def _record_observed(self, label: str, value: Any) -> None:
        self._last_observed = _diagnostic_value(label, value)

    def _advance_finalize_one(self) -> None:
        """Advance the formerly monolithic material assembly one primitive.

        The bytes contributing to the snapshot digest are unchanged; this
        only makes the post-scan maps and relation indexes resumable so a
        validation slice cannot spend hundreds of milliseconds in one dict
        comprehension.
        """

        state = self._finalize_state
        if state == "prepare":
            self._finalize_face_payload = tuple(self._face_payload)
            self._finalize_edge_payload = tuple(self._edge_payload)
            self._finalize_vertex_payload = tuple(self._vertex_payload)
            self._finalize_loop_payload = tuple(self._loop_sorted)
            self._finalize_island_payload = tuple(self._island_payload)
            self._finalize_island_face_keys = tuple(self._island_face_keys)
            mesh = (
                (
                    "object",
                    getattr(self.obj, "name", None),
                    getattr(getattr(self.obj, "data", None), "name", None),
                ),
                ("uv_layer", getattr(self.uv_layer, "name", None)),
                _active_state(self.context, self.obj, self.bm),
                self._finalize_face_payload,
                self._finalize_edge_payload,
                self._finalize_vertex_payload,
                self._finalize_loop_payload,
                self._finalize_island_payload,
            )
            options_wire = self.options.to_wire() if hasattr(self.options, "to_wire") else self.options
            self._finalize_mesh = mesh
            self._finalize_canonical = (
                "mc4c2-snapshot",
                self.generation,
                mesh,
                ("options", options_wire),
            )
            self._finalize_index = 0
            self._finalize_state = "face_map"
            return

        map_specs = {
            "face_map": (self._finalize_face_payload, self._finalize_face_by_key, "edge_map"),
            "edge_map": (self._finalize_edge_payload, self._finalize_edge_by_key, "vertex_map"),
            "vertex_map": (self._finalize_vertex_payload, self._finalize_vertex_by_key, "loop_map"),
            "loop_map": (self._finalize_loop_payload, self._finalize_loop_by_key, "edge_relations"),
        }
        if state in map_specs:
            items, target, next_state = map_specs[state]
            if self._finalize_index < len(items):
                item = items[self._finalize_index]
                target[item[0]] = item
                self._finalize_index += 1
                return
            self._finalize_index = 0
            self._finalize_state = next_state
            return

        if state == "edge_relations":
            if self._finalize_index < len(self._finalize_edge_payload):
                item = self._finalize_edge_payload[self._finalize_index]
                self._finalize_index += 1
                for loop_key in tuple(item[1]):
                    self._finalize_edge_items_by_loop[loop_key] = (
                        self._finalize_edge_items_by_loop.get(loop_key, ()) + (item,)
                    )
                return
            self._finalize_index = 0
            self._finalize_state = "vertex_relations"
            return

        if state == "vertex_relations":
            if self._finalize_index < len(self._finalize_vertex_payload):
                item = self._finalize_vertex_payload[self._finalize_index]
                self._finalize_index += 1
                for loop_key in tuple(item[1]):
                    self._finalize_vertex_items_by_loop[loop_key] = (
                        self._finalize_vertex_items_by_loop.get(loop_key, ()) + (item,)
                    )
                return
            self._finalize_index = 0
            self._finalize_state = "material"
            return

        if state == "material":
            material = SnapshotMaterial(
                mesh=self._finalize_mesh,
                face_payload=self._finalize_face_payload,
                edge_payload=self._finalize_edge_payload,
                vertex_payload=self._finalize_vertex_payload,
                loop_payload=self._finalize_loop_payload,
                island_payload=self._finalize_island_payload,
                island_face_keys=self._finalize_island_face_keys,
                face_by_key=MappingProxyType(self._finalize_face_by_key),
                edge_by_key=MappingProxyType(self._finalize_edge_by_key),
                vertex_by_key=MappingProxyType(self._finalize_vertex_by_key),
                loop_by_key=MappingProxyType(self._finalize_loop_by_key),
                edge_items_by_loop=MappingProxyType(self._finalize_edge_items_by_loop),
                vertex_items_by_loop=MappingProxyType(self._finalize_vertex_items_by_loop),
            )
            identity = SnapshotIdentity(
                self.session_nonce,
                self.generation,
                self._digest.hexdigest().upper(),
            )
            self._result = SnapshotCapture(identity, self._finalize_canonical, material)
            self._finalize_state = "done"
            self._set_phase("done")
            return
        if state == "done":
            return
        raise ProcessAdapterError("unknown incremental snapshot finalization state: %s" % state)

    def _primitive_marker(self) -> dict[str, Any]:
        phase = str(self._phase)
        if phase == "faces":
            return {"kind": "face", "phase": phase, "index": len(self._faces_source), "key": None}
        if phase == "face_loops":
            if self._current_face is None:
                return {"kind": "face_setup", "phase": phase, "index": self._face_index, "key": None}
            return {
                "kind": "loop" if self._current_loop_index < len(self._current_face_loops) else "face_record",
                "phase": phase,
                "index": self._current_loop_index,
                "key": (_index(self._current_face), self._current_loop_index),
            }
        if phase == "uv_splits":
            return {"kind": "uv_split", "phase": phase, "index": self._vertex_index, "key": None}
        if phase == "edge_records":
            key = self._edge_keys[self._edge_index] if self._edge_index < len(self._edge_keys) else None
            return {"kind": "edge_record", "phase": phase, "index": self._edge_index, "key": key}
        if phase == "vertex_records":
            key = self._vertex_keys[self._vertex_index] if self._vertex_index < len(self._vertex_keys) else None
            return {"kind": "vertex_record", "phase": phase, "index": self._vertex_index, "key": key}
        if phase == "loop_records":
            return {"kind": "loop_record", "phase": phase, "index": self._loop_index, "key": None}
        if phase == "islands":
            return {"kind": "island", "phase": phase, "index": len(self._island_payload), "key": None}
        if phase == "finalize":
            return {
                "kind": "finalize",
                "phase": phase,
                "index": self._finalize_index,
                "key": self._finalize_state,
            }
        return {"kind": phase, "phase": phase, "index": 0, "key": None}

    def _capture_failure(self, exc: BaseException, operations: int) -> None:
        self.failure_diagnostics = {
            "phase": str(self._phase),
            "primitive": dict(self.current_primitive),
            "observed": dict(self._last_observed) if self._last_observed else None,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc)[:_DIAGNOSTIC_REPR_LIMIT],
            "traceback": traceback.format_exc(limit=24)[:8192],
            "slice_index": int(self.slices + 1),
            "operations_in_slice": int(operations),
            "total_operations": int(self.primitive_operations + operations),
        }

    def _advance_one(self) -> None:
        self.current_primitive = self._primitive_marker()
        if self._phase == "faces":
            try:
                self._faces_source.append(next(self._face_iter))
            except StopIteration:
                self._faces = tuple(sorted(self._faces_source, key=lambda item: _index(item)))
                if not self._faces:
                    raise ProcessAdapterError("invalid_record_empty_mesh")
                self._set_phase("face_loops")
            return

        if self._phase == "face_loops":
            if self._current_face is None:
                if self._face_index >= len(self._faces):
                    self._edge_keys = tuple(sorted(self._edge_loops))
                    self._vertex_keys = tuple(sorted(self._vertex_loops))
                    self._set_phase("uv_splits")
                    return
                self._current_face = self._faces[self._face_index]
                self._current_face_loops = getattr(self._current_face, "loops", ()) or ()
                self._current_loop_index = 0
                self._face_loop_keys[_index(self._current_face)] = ()
                return
            loops = self._current_face_loops
            face_key = _index(self._current_face)
            if self._current_loop_index < len(loops):
                local_index = self._current_loop_index
                loop = loops[local_index]
                self._current_loop_index += 1
                self._record_observed("uv_layer", self.uv_layer)
                self._record_observed("loop", loop)
                key = (face_key, int(local_index))
                next_key = (face_key, (local_index + 1) % max(1, len(loops)))
                prev_key = (face_key, (local_index - 1) % max(1, len(loops)))
                uv = _uv_pair(loop, self.uv_layer)
                selected = _selected_loop_state(loop, self._current_face, self.uv_layer)
                edge_key = _index(getattr(loop, "edge", None))
                vertex_key = _index(getattr(loop, "vert", None))
                self._record_observed("edge", getattr(loop, "edge", None))
                self._record_observed("vertex", getattr(loop, "vert", None))
                raw = (
                    key,
                    face_key,
                    edge_key,
                    vertex_key,
                    next_key,
                    prev_key,
                    uv,
                    selected,
                )
                self._loop_payload.append(raw)
                self._loop_payload_by_key[key] = raw
                self._loop_objects[key] = loop
                self._loop_key_by_id[id(loop)] = key
                self._live_loop_map[key] = loop
                self._prewrite_snapshot[key] = uv
                self._selection_snapshot[key] = selected
                self._edge_loops.setdefault(edge_key, []).append(key)
                self._edge_objects.setdefault(edge_key, getattr(loop, "edge", None))
                self._vertex_loops.setdefault(vertex_key, []).append(key)
                self._vertex_objects.setdefault(vertex_key, getattr(loop, "vert", None))
                self._vertex_uvs.setdefault(vertex_key, set()).add(uv)
                return
            keys = tuple((face_key, index) for index in range(len(loops)))
            self._face_loop_keys[face_key] = keys
            face_payload = (face_key, keys, bool(getattr(self._current_face, "select", False)))
            self._face_payload.append(face_payload)
            self._digest_update(("face", face_payload))
            self._face_index += 1
            self._current_face = None
            self._current_face_loops = None
            return

        if self._phase == "uv_splits":
            if self._vertex_index < len(self._vertex_keys):
                key = self._vertex_keys[self._vertex_index]
                self._vertex_index += 1
                self._uv_split[key] = len(self._vertex_uvs.get(key, ())) > 1
                return
            self._vertex_index = 0
            self._set_phase("edge_records")
            return

        if self._phase == "edge_records":
            if self._edge_index < len(self._edge_keys):
                edge_key = self._edge_keys[self._edge_index]
                self._edge_index += 1
                keys = tuple(sorted(self._edge_loops[edge_key]))
                face_keys = tuple(sorted({self._loop_objects[key].face.index for key in keys}))
                edge = self._edge_objects.get(edge_key)
                payload = (
                    edge_key,
                    keys,
                    tuple(int(value) for value in face_keys),
                    len(face_keys) == 1,
                    len(getattr(edge, "link_faces", ())) > 2,
                    bool(getattr(edge, "seam", False)),
                )
                self._edge_payload.append(payload)
                self._edge_boundary_by_key[edge_key] = bool(len(face_keys) == 1)
                self._digest_update(("edge", payload))
                return
            self._edge_index = 0
            self._set_phase("vertex_records")
            return

        if self._phase == "vertex_records":
            if self._vertex_index < len(self._vertex_keys):
                vertex_key = self._vertex_keys[self._vertex_index]
                self._vertex_index += 1
                keys = tuple(sorted(self._vertex_loops[vertex_key]))
                payload = (
                    vertex_key,
                    keys,
                    any(
                        self._edge_boundary_by_key.get(
                            int(self._loop_payload_by_key[key][2]),
                            False,
                        )
                        for key in keys
                    ),
                    self._uv_split[vertex_key],
                    bool(getattr(self._vertex_objects.get(vertex_key), "select", False)),
                )
                self._vertex_payload.append(payload)
                self._digest_update(("vertex", payload))
                return
            self._vertex_index = 0
            self._loop_sorted = tuple(sorted(self._loop_payload, key=lambda item: item[0]))
            self._set_phase("loop_records")
            return

        if self._phase == "loop_records":
            if self._loop_index < len(self._loop_sorted):
                raw = self._loop_sorted[self._loop_index]
                self._loop_index += 1
                self._digest_update(("loop", raw))
                return
            self._loop_index = 0
            self._set_phase("islands")
            return

        if self._phase == "islands":
            try:
                island = tuple(next(self._island_iter))
            except StopIteration:
                self._island_payload.sort(key=repr)
                self._island_face_keys.sort(key=repr)
                for payload in self._island_payload:
                    self._digest_update(("island", payload))
                self._set_phase("finalize")
                return
            loop_keys = tuple(sorted(self._loop_key_by_id[id(loop)] for loop in island))
            self._island_payload.append(loop_keys)
            face_keys = tuple(sorted({int(key[0]) for key in loop_keys}))
            self._island_face_keys.append((face_keys, loop_keys))
            return

        if self._phase == "finalize":
            self._advance_finalize_one()
            return
        if self._phase == "done":
            return
        raise ProcessAdapterError("unknown incremental snapshot phase: %s" % self._phase)

    def advance(self, operation_budget: int = 64, deadline: Optional[float] = None) -> Optional[SnapshotCapture]:
        if self.done:
            return self._result
        try:
            budget = max(1, int(operation_budget))
        except (TypeError, ValueError):
            budget = 1
        started = time.perf_counter()
        operations = 0
        try:
            while not self.done and operations < budget:
                if deadline is not None and time.perf_counter() >= deadline:
                    break
                primitive_started = time.perf_counter()
                self._advance_one()
                primitive_elapsed = (time.perf_counter() - primitive_started) * 1000.0
                self.last_primitive = dict(self.current_primitive)
                if primitive_elapsed >= self.max_primitive_ms:
                    self.max_primitive_ms = primitive_elapsed
                    self.max_primitive = dict(self.current_primitive)
                operations += 1
        except Exception as exc:
            self._capture_failure(exc, operations)
            raise
        elapsed = (time.perf_counter() - started) * 1000.0
        self.slices += 1
        self.primitive_operations += operations
        self.max_slice_ms = max(self.max_slice_ms, elapsed)
        self.elapsed_ms += elapsed
        return self._result


class SnapshotGuard:
    """Cheap sentinel plus incremental full-identity verification."""

    def __init__(
        self,
        capture: SnapshotCapture,
        context: Any,
        obj: Any,
        bm: Any,
        uv_layer: Any,
        islands: Optional[Iterable[Iterable[Any]]],
        *,
        session_nonce: str,
        generation: int,
        options: Any,
    ) -> None:
        self.capture = capture
        self.context = context
        self.obj = obj
        self.bm = bm
        self.uv_layer = uv_layer
        self.islands = islands
        self.session_nonce = str(session_nonce)
        self.generation = int(generation)
        self.options = options
        self.initial_sentinel = make_snapshot_sentinel(context, obj, bm, uv_layer)
        self._verification_builder: Optional[IncrementalSnapshotBuilder] = None
        self.validation_requested = False
        self.validation_complete = False
        self.invalid_reason = ""
        self.validation_slices = 0
        self.validation_operations = 0
        self.validation_epoch = 0
        self.validation_elapsed_ms = 0.0
        self.max_validation_slice_ms = 0.0
        self.max_validation_primitive_ms = 0.0
        self.max_validation_primitive: dict[str, Any] = {}

    def cheap_check(self) -> bool:
        if self.invalid_reason:
            return False
        current = make_snapshot_sentinel(self.context, self.obj, self.bm, self.uv_layer)
        if current != self.initial_sentinel:
            if current.uv_samples != self.initial_sentinel.uv_samples:
                self.invalid_reason = "snapshot digest sentinel changed"
            else:
                self.invalid_reason = "snapshot sentinel changed"
            return False
        return True

    def request_validation(self) -> None:
        if self.validation_complete:
            self._verification_builder = None
            self.validation_complete = False
        self.validation_requested = True

    def advance_validation(
        self,
        *,
        operation_budget: int = 64,
        deadline: Optional[float] = None,
    ) -> str:
        if not self.cheap_check():
            return "invalid"
        if self.validation_complete and not self.validation_requested:
            return "valid"
        self.validation_requested = True
        # One epoch represents one bounded continuation attempt.  It is
        # intentionally discrete (never wall-clock data) so the session
        # watchdog can distinguish a live validation continuation from an
        # idle pipeline without weakening the digest gate.
        self.validation_epoch += 1
        if self._verification_builder is None:
            self._verification_builder = IncrementalSnapshotBuilder(
                self.context,
                self.obj,
                self.bm,
                self.uv_layer,
                self.islands,
                session_nonce=self.session_nonce,
                generation=self.generation,
                options=self.options,
            )
        before = self._verification_builder.elapsed_ms
        started = time.perf_counter()
        result = self._verification_builder.advance(operation_budget, deadline)
        elapsed = (time.perf_counter() - started) * 1000.0
        self.validation_elapsed_ms += elapsed
        self.validation_slices = self._verification_builder.slices
        self.validation_operations = self._verification_builder.primitive_operations
        self.max_validation_slice_ms = max(
            self.max_validation_slice_ms,
            self._verification_builder.max_slice_ms,
        )
        if self._verification_builder.max_primitive_ms >= self.max_validation_primitive_ms:
            self.max_validation_primitive_ms = float(
                self._verification_builder.max_primitive_ms
            )
            self.max_validation_primitive = dict(
                self._verification_builder.max_primitive
            )
        del before
        if result is None:
            return "pending"
        if result.identity.snapshot_digest != self.capture.identity.snapshot_digest:
            self.invalid_reason = "full snapshot digest changed"
            return "invalid"
        self.validation_complete = True
        self.validation_requested = False
        return "valid"


class _SnapshotBoundaryBuilder:
    """Resumable pure boundary tracing for one immutable graph payload."""

    def __init__(self, loops: Mapping[Any, GraphLoopData]) -> None:
        self.loops = loops
        self.keys = tuple(sorted(key for key, value in loops.items() if value.boundary))
        self.phase = "endpoints" if self.keys else "done"
        self.index = 0
        self.endpoints: dict[Any, tuple[Any, Any]] = {}
        self.endpoint_items: tuple[Any, ...] = ()
        self.endpoint_to_loops: dict[Any, list[Any]] = {}
        self.adjacency: dict[Any, set[Any]] = {}
        self.unseen: set[Any] = set()
        self.stack: list[Any] = []
        self.dfs_neighbors: Optional[tuple[Any, ...]] = None
        self.dfs_neighbor_index = 0
        self.current_component: list[Any] = []
        self.components: list[tuple[Any, ...]] = []
        self.current_component_index = 0
        self.ordered_components: list[tuple[float, tuple[Any, ...]]] = []
        self.current_order: list[Any] = []
        self.current_start: Any = None
        self.current: Any = None
        self.previous: Any = None
        self.area_index = 0
        self.area_twice = 0.0
        self.boundaries: tuple[GraphBoundaryData, ...] = ()

    @property
    def done(self) -> bool:
        return self.phase == "done"

    def _advance_one(self) -> None:
        if self.phase == "endpoints":
            key = self.keys[self.index]
            self.index += 1
            loop = self.loops[key]
            next_loop = self.loops.get(loop.next_key)
            if next_loop is None:
                raise ProcessAdapterError("snapshot boundary next loop missing")
            # The legacy path joins boundary segments by mesh vertex plus a
            # rounded UV token.  Raw float tuples are not equivalent around a
            # seam and can create a false branch/open diagnostic.
            start = (loop.vertex_key, _legacy_uv_token(loop.uv))
            end = (next_loop.vertex_key, _legacy_uv_token(next_loop.uv))
            self.endpoints[key] = (start, end)
            self.endpoint_to_loops.setdefault(start, []).append(key)
            self.endpoint_to_loops.setdefault(end, []).append(key)
            if self.index >= len(self.keys):
                self.endpoint_items = tuple(sorted(self.endpoint_to_loops.items(), key=repr))
                self.adjacency = {item: set() for item in self.keys}
                self.index = 0
                self.phase = "adjacency"
            return
        if self.phase == "adjacency":
            endpoint, values = self.endpoint_items[self.index]
            del endpoint
            self.index += 1
            if len(values) != 2:
                raise ProcessAdapterError("boundary_component_branch_or_open")
            left, right = values
            if left == right:
                raise ProcessAdapterError("boundary_component_degenerate_segment")
            self.adjacency[left].add(right)
            self.adjacency[right].add(left)
            if self.index >= len(self.endpoint_items):
                self.index = 0
                self.phase = "validate"
            return
        if self.phase == "validate":
            key = self.keys[self.index]
            self.index += 1
            if len(self.adjacency[key]) != 2:
                raise ProcessAdapterError("boundary_component_not_closed")
            if self.index >= len(self.keys):
                self.unseen = set(self.keys)
                self.phase = "components"
                self.index = 0
            return
        if self.phase == "components":
            if self.dfs_neighbors is not None:
                if self.dfs_neighbor_index < len(self.dfs_neighbors):
                    neighbour = self.dfs_neighbors[self.dfs_neighbor_index]
                    self.dfs_neighbor_index += 1
                    if neighbour in self.unseen:
                        self.unseen.remove(neighbour)
                        self.stack.append(neighbour)
                else:
                    self.dfs_neighbors = None
                    self.dfs_neighbor_index = 0
                return
            if self.stack:
                key = self.stack.pop()
                self.current_component.append(key)
                self.dfs_neighbors = tuple(sorted(self.adjacency[key]))
                self.dfs_neighbor_index = 0
                return
            if self.current_component:
                self.components.append(tuple(sorted(self.current_component)))
                self.current_component = []
                return
            if self.unseen:
                start = min(self.unseen)
                self.unseen.remove(start)
                self.stack = [start]
                return
            self.current_component_index = 0
            self.phase = "order_init"
            return
        if self.phase == "order_init":
            if self.current_component_index >= len(self.components):
                self.ordered_components.sort(key=lambda item: (-item[0], item[1]))
                self.index = 0
                self.phase = "boundary_records"
                return
            component = self.components[self.current_component_index]
            self.current_start = min(component)
            self.current = self.current_start
            self.previous = None
            self.current_order = [self.current_start]
            self.area_index = 0
            self.area_twice = 0.0
            self.phase = "order_trace"
            return
        if self.phase == "order_trace":
            component = self.components[self.current_component_index]
            if len(self.current_order) < len(component):
                candidates = sorted(
                    neighbour
                    for neighbour in self.adjacency[self.current]
                    if neighbour != self.previous and neighbour not in self.current_order
                )
                if not candidates:
                    raise ProcessAdapterError("boundary_component_trace_failed")
                self.previous, self.current = self.current, candidates[0]
                self.current_order.append(self.current)
                return
            if self.current_start not in self.adjacency[self.current]:
                raise ProcessAdapterError("boundary_component_trace_not_closed")
            self.phase = "order_area"
            return
        if self.phase == "order_area":
            component = self.components[self.current_component_index]
            if self.area_index < len(component):
                key = component[self.area_index]
                self.area_index += 1
                loop = self.loops[key]
                next_loop = self.loops[loop.next_key]
                self.area_twice += loop.uv[0] * next_loop.uv[1]
                self.area_twice -= next_loop.uv[0] * loop.uv[1]
                return
            area = abs(self.area_twice) * 0.5
            if not math.isfinite(area) or area <= 1.0e-14:
                raise ProcessAdapterError("boundary_component_degenerate_area")
            self.ordered_components.append((area, tuple(self.current_order)))
            self.current_component_index += 1
            self.phase = "order_init"
            return
        if self.phase == "boundary_records":
            if self.index >= len(self.ordered_components):
                self.boundaries = tuple(self.boundaries)
                self.phase = "done"
                return
            _area, loop_keys = self.ordered_components[self.index]
            index = self.index
            self.index += 1
            self.boundaries += (
                GraphBoundaryData(
                    key=("boundary", index),
                    loop_keys=tuple(loop_keys),
                    role="outer" if index == 0 else "hole",
                    parent_key=None if index == 0 else ("boundary", 0),
                    signature=("closed", len(loop_keys)),
                ),
            )
            return
        if self.phase == "done":
            return
        raise ProcessAdapterError("unknown snapshot boundary phase: %s" % self.phase)

    def advance(self, operation_budget: int, deadline: Optional[float]) -> None:
        operations = 0
        while not self.done and operations < max(1, int(operation_budget)):
            if deadline is not None and time.perf_counter() >= deadline:
                break
            self._advance_one()
            operations += 1


class SnapshotGraphBuilder:
    """Build one GraphData from the immutable snapshot, never from BMesh."""

    def __init__(self, capture: SnapshotCapture, island_key: Any, live_loop_map: Mapping[Any, Any]) -> None:
        if capture.material is None:
            raise ProcessAdapterError("snapshot has no immutable graph material")
        self.capture = capture
        self.material: SnapshotMaterial = capture.material
        self.face_keys = frozenset(int(value) for value in tuple(island_key))
        self.live_loop_map = live_loop_map
        self.phase = "faces"
        self._index = 0
        self._faces: list[GraphFaceData] = []
        self._edges: list[GraphEdgeData] = []
        self._vertices: list[GraphVertexData] = []
        self._loops: dict[Any, GraphLoopData] = {}
        self._loop_items: tuple[Any, ...] = ()
        self._local_edge_loop_keys: dict[Any, list[Any]] = {}
        self._local_edge_face_keys: dict[Any, set[Any]] = {}
        self._local_edge_boundary: dict[Any, bool] = {}
        self._local_vertex_loop_keys: dict[Any, list[Any]] = {}
        self._local_vertex_uv_tokens: dict[Any, set[tuple[float, float]]] = {}
        self._local_vertex_boundary: dict[Any, bool] = {}
        self._local_vertex_split: dict[Any, bool] = {}
        self._canonical_faces: list[Any] = []
        self._canonical_edges: list[Any] = []
        self._canonical_vertices: list[Any] = []
        self._canonical_loops: list[Any] = []
        self._canonical_boundaries: list[Any] = []
        self._boundary_builder: Optional[_SnapshotBoundaryBuilder] = None
        self.result: Optional[tuple[GraphData, dict[Any, Any]]] = None
        self.slices = 0
        self.primitive_operations = 0
        self.max_slice_ms = 0.0
        self.elapsed_ms = 0.0
        self.max_primitive_ms = 0.0
        self.max_primitive: dict[str, Any] = {}
        self.last_primitive: dict[str, Any] = {}

    @property
    def done(self) -> bool:
        return self.result is not None

    def _advance_one(self) -> None:
        self.last_primitive = {
            "phase": str(self.phase),
            "kind": str(self.phase),
            "index": int(self._index),
            "key": None,
        }
        if self.phase == "edges" and self._index < len(self.material.edge_payload):
            self.last_primitive["key"] = self.material.edge_payload[self._index][0]
        elif self.phase == "vertices" and self._index < len(self.material.vertex_payload):
            self.last_primitive["key"] = self.material.vertex_payload[self._index][0]
        elif self.phase == "loops" and self._index < len(self._loop_items):
            self.last_primitive["key"] = self._loop_items[self._index][0]
        elif self.phase == "loop_semantics" and self._index < len(self._loop_items):
            self.last_primitive["key"] = self._loop_items[self._index][0]
        if self.phase == "faces":
            if self._index < len(self.material.face_payload):
                item = self.material.face_payload[self._index]
                self._index += 1
                if int(item[0]) in self.face_keys:
                    face = GraphFaceData(item[0], tuple(item[1]), ())
                    self._faces.append(face)
                    self._canonical_faces.append(_payload_canonical(face.to_wire()))
                return
            self._index = 0
            self.phase = "loops"
            self._loop_items = tuple(self.material.loop_by_key.items())
            return
        if self.phase == "loops":
            if self._index < len(self._loop_items):
                _key, item = self._loop_items[self._index]
                self._index += 1
                if int(item[1]) not in self.face_keys:
                    return
                edge = self.material.edge_by_key[item[2]]
                loop = GraphLoopData(
                    key=item[0],
                    face_key=item[1],
                    edge_key=item[2],
                    vertex_key=item[3],
                    next_key=item[4],
                    prev_key=item[5],
                    uv=tuple(item[6]),
                    boundary=False,
                    seam=False,
                    signature=("uv_split", False),
                )
                self._loops[loop.key] = loop
                self._local_edge_loop_keys.setdefault(loop.edge_key, []).append(loop.key)
                self._local_edge_face_keys.setdefault(loop.edge_key, set()).add(loop.face_key)
                self._local_vertex_loop_keys.setdefault(loop.vertex_key, []).append(loop.key)
                self._local_vertex_uv_tokens.setdefault(loop.vertex_key, set()).add(
                    _legacy_uv_token(loop.uv)
                )
                return
            self._index = 0
            self.phase = "edges"
            return
        if self.phase == "edges":
            if self._index < len(self.material.edge_payload):
                item = self.material.edge_payload[self._index]
                self._index += 1
                face_keys = tuple(value for value in item[2] if int(value) in self.face_keys)
                loop_keys = tuple(sorted(key for key in item[1] if key in self._loops))
                if loop_keys:
                    boundary = len(face_keys) == 1
                    self._local_edge_boundary[item[0]] = boundary
                    edge_data = GraphEdgeData(
                        key=item[0],
                        loop_keys=loop_keys,
                        face_keys=tuple(sorted(int(value) for value in face_keys)),
                        boundary=boundary,
                        non_manifold=bool(item[4]),
                        signature=("mesh_non_manifold", bool(item[4])),
                    )
                    self._edges.append(
                        edge_data
                    )
                    self._canonical_edges.append(_payload_canonical(edge_data.to_wire()))
                return
            self._index = 0
            self.phase = "vertices"
            return
        if self.phase == "vertices":
            if self._index < len(self.material.vertex_payload):
                item = self.material.vertex_payload[self._index]
                self._index += 1
                loop_keys = tuple(key for key in item[1] if key in self._loops)
                if loop_keys:
                    boundary = any(
                        self._local_edge_boundary.get(self._loops[key].edge_key, False)
                        for key in loop_keys
                    )
                    split = len(self._local_vertex_uv_tokens.get(item[0], ())) > 1
                    self._local_vertex_boundary[item[0]] = boundary
                    self._local_vertex_split[item[0]] = split
                    vertex_data = GraphVertexData(
                        key=item[0],
                        loop_keys=tuple(sorted(loop_keys)),
                        boundary=boundary,
                        signature=("uv_split", split),
                    )
                    self._vertices.append(vertex_data)
                    self._canonical_vertices.append(_payload_canonical(vertex_data.to_wire()))
                return
            self._index = 0
            self._loop_items = tuple(sorted(self._loops.items(), key=lambda item: repr(item[0])))
            self.phase = "loop_semantics"
            return
        if self.phase == "loop_semantics":
            if self._index < len(self._loop_items):
                key, loop = self._loop_items[self._index]
                self._index += 1
                boundary = self._local_edge_boundary.get(loop.edge_key, False)
                split = self._local_vertex_split.get(loop.vertex_key, False)
                updated = GraphLoopData(
                    key=loop.key,
                    face_key=loop.face_key,
                    edge_key=loop.edge_key,
                    vertex_key=loop.vertex_key,
                    next_key=loop.next_key,
                    prev_key=loop.prev_key,
                    uv=loop.uv,
                    boundary=boundary,
                    seam=split,
                    signature=("uv_split", split),
                )
                self._loops[key] = updated
                self._canonical_loops.append(_payload_canonical(updated.to_wire()))
                return
            self._index = 0
            self._boundary_builder = _SnapshotBoundaryBuilder(self._loops)
            self.phase = "boundaries"
            return
        if self.phase == "boundaries":
            assert self._boundary_builder is not None
            self._boundary_builder.advance(1, self._deadline)
            if self._boundary_builder.done:
                self._canonical_boundaries = [
                    _payload_canonical(item.to_wire())
                    for item in self._boundary_builder.boundaries
                ]
                self.phase = "finalize"
            return
        if self.phase == "finalize":
            assert self._boundary_builder is not None
            faces = tuple(self._faces)
            edges = tuple(self._edges)
            vertices = tuple(self._vertices)
            loops = tuple(self._loops[key] for key, _item in self._loop_items)
            boundaries = self._boundary_builder.boundaries
            # Keep the key representation identical to graph_data_from_topology
            # callers, which receive the canonical sorted tuple rather than a
            # frozenset produced by the builder's membership filter.
            graph_key = graph_key_for_island(tuple(sorted(self.face_keys)))
            content_digest = _graph_content_digest(
                graph_key,
                faces,
                edges,
                vertices,
                loops,
                boundaries,
                (
                    tuple(self._canonical_faces),
                    tuple(self._canonical_edges),
                    tuple(self._canonical_vertices),
                    tuple(self._canonical_loops),
                    tuple(self._canonical_boundaries),
                ),
            )
            data = GraphData(
                graph_key=graph_key,
                faces=faces,
                edges=edges,
                vertices=vertices,
                loops=loops,
                boundaries=boundaries,
                content_digest=content_digest,
            )
            live = {
                key: self.live_loop_map[key]
                for key in self._loops
                if key in self.live_loop_map
            }
            self.result = (data, live)
            self.phase = "done"
            return
        if self.phase == "done":
            return
        raise ProcessAdapterError("unknown snapshot graph phase: %s" % self.phase)

    def advance(self, operation_budget: int = 64, deadline: Optional[float] = None) -> Optional[tuple[GraphData, dict[Any, Any]]]:
        if self.done:
            return self.result
        try:
            budget = max(1, int(operation_budget))
        except (TypeError, ValueError):
            budget = 1
        self._deadline = deadline
        started = time.perf_counter()
        operations = 0
        while not self.done and operations < budget:
            if deadline is not None and time.perf_counter() >= deadline:
                break
            primitive_started = time.perf_counter()
            self._advance_one()
            primitive_elapsed = (time.perf_counter() - primitive_started) * 1000.0
            self.last_primitive = dict(getattr(self, "last_primitive", {}))
            self.last_primitive.setdefault("phase", self.phase)
            if primitive_elapsed >= self.max_primitive_ms:
                self.max_primitive_ms = primitive_elapsed
                self.max_primitive = dict(self.last_primitive)
            operations += 1
        elapsed = (time.perf_counter() - started) * 1000.0
        self.slices += 1
        self.primitive_operations += operations
        self.max_slice_ms = max(self.max_slice_ms, elapsed)
        self.elapsed_ms += elapsed
        return self.result


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProcessAdapterError(f"{name} is not numeric") from exc
    if not math.isfinite(result):
        raise ProcessAdapterError(f"{name} is not finite")
    return result


def _index(value: Any, fallback: int = -1) -> int:
    try:
        return int(getattr(value, "index", value))
    except (TypeError, ValueError):
        return int(fallback)


def _uv_pair(loop: Any, uv_layer: Any) -> tuple[float, float]:
    try:
        uv = loop[uv_layer].uv
        return (_number(uv.x, "uv.x"), _number(uv.y, "uv.y"))
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProcessAdapterError("could not read a BMesh UV loop") from exc


def _selected_loop_state(loop: Any, face: Any, uv_layer: Any) -> tuple[bool, ...]:
    try:
        luv = loop[uv_layer]
        return (
            bool(getattr(luv, "select", False)),
            bool(getattr(luv, "select_edge", False)),
            bool(getattr(face, "select", False)),
            bool(getattr(loop.vert, "select", False)),
            bool(getattr(loop.edge, "select", False)),
        )
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        raise ProcessAdapterError("could not read BMesh selection state") from exc


def _sorted_faces(bm: Any) -> tuple[Any, ...]:
    faces = tuple(getattr(bm, "faces", ()) or ())
    return tuple(sorted(faces, key=lambda face: _index(face)))


def _selected_object_names(context: Any) -> tuple[str, ...]:
    selected = getattr(context, "selected_objects", ()) if context is not None else ()
    if not selected and context is not None:
        view_layer = getattr(context, "view_layer", None)
        objects = getattr(view_layer, "objects", ()) if view_layer is not None else ()
        selected = tuple(
            item
            for item in objects
            if bool(getattr(item, "select_get", lambda: False)())
        )
    return tuple(sorted(str(getattr(item, "name", "")) for item in selected))


def _active_state(context: Any, obj: Any, bm: Any) -> tuple[Any, ...]:
    active_face = getattr(getattr(bm, "faces", None), "active", None)
    uv_layers = getattr(getattr(obj, "data", None), "uv_layers", None)
    active_uv = getattr(uv_layers, "active", None)
    return (
        getattr(obj, "name", None),
        getattr(getattr(obj, "data", None), "name", None),
        getattr(obj, "mode", None),
        None if active_face is None else _index(active_face),
        getattr(active_uv, "name", None),
        _selected_object_names(context),
    )


def _island_key_payload(islands: Optional[Iterable[Iterable[Any]]]) -> tuple[Any, ...]:
    if islands is None:
        return ()
    values = []
    for island in islands:
        keys = []
        for loop in island:
            face = getattr(loop, "face", None)
            face_key = _index(face)
            local_key = None
            for local_index, candidate in enumerate(getattr(face, "loops", ()) or ()):
                if candidate is loop:
                    local_key = local_index
                    break
            if local_key is None:
                raise ProcessAdapterError("island contains a loop outside its face cycle")
            keys.append((face_key, int(local_key)))
        values.append(tuple(sorted(keys)))
    return tuple(sorted(values, key=repr))


def _mesh_canonical(
    context: Any,
    obj: Any,
    bm: Any,
    uv_layer: Any,
    islands: Optional[Iterable[Iterable[Any]]],
) -> tuple[Any, ...]:
    """Copy topology, UV, selection and active state into primitives only."""

    faces = _sorted_faces(bm)
    face_payload = []
    loop_payload = []
    edge_loops: dict[int, list[tuple[int, int]]] = {}
    edge_faces: dict[int, set[int]] = {}
    edge_objects: dict[int, Any] = {}
    vertex_loops: dict[int, list[tuple[int, int]]] = {}
    vertex_uvs: dict[int, set[tuple[float, float]]] = {}
    vertex_objects: dict[int, Any] = {}
    for face in faces:
        face_key = _index(face)
        face_loops = tuple(getattr(face, "loops", ()) or ())
        keys = []
        for local_index, loop in enumerate(face_loops):
            key = (face_key, int(local_index))
            keys.append(key)
            edge_key = _index(getattr(loop, "edge", None))
            vertex_key = _index(getattr(loop, "vert", None))
            next_key = (face_key, (local_index + 1) % max(1, len(face_loops)))
            prev_key = (face_key, (local_index - 1) % max(1, len(face_loops)))
            uv = _uv_pair(loop, uv_layer)
            edge_loops.setdefault(edge_key, []).append(key)
            edge_faces.setdefault(edge_key, set()).add(face_key)
            edge_objects.setdefault(edge_key, getattr(loop, "edge", None))
            vertex_loops.setdefault(vertex_key, []).append(key)
            vertex_uvs.setdefault(vertex_key, set()).add(uv)
            vertex_objects.setdefault(vertex_key, getattr(loop, "vert", None))
            loop_payload.append(
                (
                    key,
                    face_key,
                    edge_key,
                    vertex_key,
                    next_key,
                    prev_key,
                    uv,
                    _selected_loop_state(loop, face, uv_layer),
                )
            )
        face_payload.append((face_key, tuple(keys), bool(getattr(face, "select", False))))

    edge_payload = []
    for edge_key in sorted(edge_loops):
        loops = tuple(sorted(edge_loops[edge_key]))
        faces_for_edge = tuple(sorted(edge_faces.get(edge_key, ())))
        edge = edge_objects.get(edge_key)
        linked_faces = getattr(edge, "link_faces", ()) if edge is not None else ()
        edge_payload.append(
            (
                edge_key,
                loops,
                faces_for_edge,
                len(faces_for_edge) == 1,
                len(tuple(linked_faces)) > 2,
                bool(getattr(edge, "seam", False)) if edge is not None else False,
            )
        )

    vertex_payload = []
    for vertex_key in sorted(vertex_loops):
        loops = tuple(sorted(vertex_loops[vertex_key]))
        vertex = vertex_objects.get(vertex_key)
        linked_edges = getattr(vertex, "link_edges", ()) if vertex is not None else ()
        vertex_payload.append(
            (
                vertex_key,
                loops,
                any(len(edge_faces.get(_index(edge), ())) == 1 for edge in linked_edges),
                len(vertex_uvs.get(vertex_key, ())) > 1,
                bool(getattr(vertex, "select", False)) if vertex is not None else False,
            )
        )

    loop_payload.sort(key=lambda item: item[0])
    return (
        ("object", getattr(obj, "name", None), getattr(getattr(obj, "data", None), "name", None)),
        ("uv_layer", getattr(uv_layer, "name", None)),
        ("active", _active_state(context, obj, bm)),
        ("faces", tuple(sorted(face_payload))),
        ("edges", tuple(edge_payload)),
        ("vertices", tuple(vertex_payload)),
        ("loops", tuple(loop_payload)),
        ("islands", _island_key_payload(islands)),
    )


def capture_snapshot(
    context: Any,
    obj: Any,
    bm: Any,
    uv_layer: Any,
    islands: Optional[Iterable[Iterable[Any]]],
    *,
    session_nonce: str,
    generation: int,
    options: Any,
) -> SnapshotCapture:
    """Capture a deterministic identity without retaining Blender objects."""

    canonical = (
        "mc3a-snapshot",
        int(generation),
        _mesh_canonical(context, obj, bm, uv_layer, islands),
        ("options", options.to_wire() if hasattr(options, "to_wire") else options),
    )
    digest = stable_digest(canonical)
    return SnapshotCapture(
        identity=SnapshotIdentity(session_nonce, int(generation), digest),
        canonical=canonical,
    )


def graph_key_for_island(island_key: Any) -> str:
    """Return a stable key that is independent of object identity."""

    return "island-" + stable_digest(("graph-key", island_key))[:32]


def graph_data_from_topology(graph: Any, island_key: Any) -> GraphData:
    """Copy one pure :class:`IslandGraph` into its wire-only representation."""

    data = GraphData.from_topology(graph, graph_key_for_island(island_key))
    data.validate()
    return data


def graph_material_wire_for_island(
    capture: SnapshotCapture,
    island_key: Any,
) -> tuple[Any, ...]:
    """Project one island from the immutable snapshot into worker primitives.

    The full snapshot is captured once on Blender's main thread.  This
    projection sends only the face/loop/edge/vertex records reachable from the
    requested island; no ``live_loop_map`` or Blender object is serialized.
    The worker can therefore reconstruct the same ``SnapshotGraphBuilder``
    input without probing the live BMesh again.
    """

    material = capture.material
    if not isinstance(material, SnapshotMaterial):
        raise ProcessAdapterError("snapshot has no immutable graph material")
    face_keys = tuple(sorted({int(value) for value in tuple(island_key)}))
    face_set = set(face_keys)
    declared_island_loops: Optional[set[Any]] = None
    for declared_faces, declared_loops in material.island_face_keys:
        if tuple(sorted(int(value) for value in tuple(declared_faces))) == face_keys:
            declared_island_loops = set(declared_loops)
            break
    faces = tuple(item for item in material.face_payload if int(item[0]) in face_set)
    loops = tuple(
        item for item in material.loop_payload
        if int(item[1]) in face_set
        and (declared_island_loops is None or item[0] in declared_island_loops)
    )
    loop_keys = {item[0] for item in loops}
    indexed_edges: dict[Any, Any] = {}
    indexed_vertices: dict[Any, Any] = {}
    if material.edge_items_by_loop and material.vertex_items_by_loop:
        for loop_key in loop_keys:
            for item in material.edge_items_by_loop.get(loop_key, ()):
                indexed_edges[item[0]] = item
            for item in material.vertex_items_by_loop.get(loop_key, ()):
                indexed_vertices[item[0]] = item
    else:
        # Compatibility for older/focused captures that predate the indexes.
        for item in material.edge_payload:
            if any(loop_key in loop_keys for loop_key in tuple(item[1])):
                indexed_edges[item[0]] = item
        for item in material.vertex_payload:
            if any(loop_key in loop_keys for loop_key in tuple(item[1])):
                indexed_vertices[item[0]] = item
    # Preserve the snapshot's canonical payload order.  The adjacency index
    # only answers membership; iterating its insertion order would make
    # worker graph record order depend on the first loop encountered and can
    # change deterministic exact tie-breaking.
    edge_keys = set(indexed_edges)
    vertex_keys = set(indexed_vertices)
    edges = tuple(item for item in material.edge_payload if item[0] in edge_keys)
    vertices = tuple(item for item in material.vertex_payload if item[0] in vertex_keys)
    island_loops = tuple(sorted(loop_keys, key=repr))
    return (
        "mc4c8-graph-material-v1",
        (),
        tuple(faces),
        tuple(edges),
        tuple(vertices),
        tuple(sorted(loops, key=lambda item: repr(item[0]))),
        (island_loops,),
        ((face_keys, island_loops),),
    )


def graph_material_wire_for_material(
    material: SnapshotMaterial,
    island_key: Any,
) -> tuple[Any, ...]:
    """Project one island from a worker-resident material context.

    This deliberately reuses the exact projection semantics of the legacy
    capture adapter, but is called only in the pure worker after the complete
    context has been loaded.  The Blender main thread never invokes this
    helper for a live process graph request.
    """

    if not isinstance(material, SnapshotMaterial):
        raise ProcessAdapterError("graph context material is invalid")
    return graph_material_wire_for_island(
        SnapshotCapture(
            identity=SnapshotIdentity("worker-context", 0, "worker-context"),
            canonical=("worker-context",),
            material=material,
        ),
        island_key,
    )


def graph_context_wire_for_capture(capture: SnapshotCapture) -> tuple[Any, ...]:
    """Return the full primitive topology context without island projection."""

    material = capture.material
    if not isinstance(material, SnapshotMaterial):
        raise ProcessAdapterError("snapshot has no immutable graph material")
    return (
        "mc4c9-graph-context-v1",
        tuple(material.mesh),
        tuple(material.face_payload),
        tuple(material.edge_payload),
        tuple(material.vertex_payload),
        tuple(material.loop_payload),
        tuple(material.island_payload),
        tuple(material.island_face_keys),
    )


def graph_context_material_from_wire(value: Any) -> SnapshotMaterial:
    """Rebuild one immutable graph context and its adjacency indexes."""

    if not isinstance(value, (tuple, list)) or len(value) != 8 or value[0] != "mc4c9-graph-context-v1":
        raise ProcessAdapterError("invalid graph context wire value")
    mesh = tuple(value[1])
    face_payload = tuple(value[2])
    edge_payload = tuple(value[3])
    vertex_payload = tuple(value[4])
    loop_payload = tuple(value[5])
    island_payload = tuple(value[6])
    island_face_keys = tuple(
        (tuple(item[0]), tuple(item[1])) for item in tuple(value[7])
    )
    edge_items_by_loop: dict[Any, list[Any]] = {}
    for item in edge_payload:
        for loop_key in tuple(item[1]):
            edge_items_by_loop.setdefault(loop_key, []).append(item)
    vertex_items_by_loop: dict[Any, list[Any]] = {}
    for item in vertex_payload:
        for loop_key in tuple(item[1]):
            vertex_items_by_loop.setdefault(loop_key, []).append(item)
    return SnapshotMaterial(
        mesh=mesh,
        face_payload=face_payload,
        edge_payload=edge_payload,
        vertex_payload=vertex_payload,
        loop_payload=loop_payload,
        island_payload=island_payload,
        island_face_keys=island_face_keys,
        face_by_key=MappingProxyType({item[0]: item for item in face_payload}),
        edge_by_key=MappingProxyType({item[0]: item for item in edge_payload}),
        vertex_by_key=MappingProxyType({item[0]: item for item in vertex_payload}),
        loop_by_key=MappingProxyType({item[0]: item for item in loop_payload}),
        edge_items_by_loop=MappingProxyType({
            loop_key: tuple(items) for loop_key, items in edge_items_by_loop.items()
        }),
        vertex_items_by_loop=MappingProxyType({
            loop_key: tuple(items) for loop_key, items in vertex_items_by_loop.items()
        }),
    )


def make_graph_context_payload(capture: SnapshotCapture) -> GraphContextPayload:
    """Create the one-per-session context payload from captured primitives."""

    wire = graph_context_wire_for_capture(capture)
    return GraphContextPayload(
        identity=capture.identity,
        context_digest=graph_context_identity_digest(capture.identity),
        material=wire,
    )


def make_fused_context_payload(
    capture: SnapshotCapture,
    descriptors: Iterable[Any],
    shape_options: ShapeOptions,
) -> GraphContextPayload:
    """Attach one immutable descriptor table to the resident graph context."""

    base = make_graph_context_payload(capture)
    descriptor_map = {}
    for descriptor in descriptors:
        wire = (
            descriptor.to_wire()
            if isinstance(descriptor, ShapeDescriptor)
            else ShapeDescriptor.from_similarity(descriptor).to_wire()
        )
        descriptor_map[str(wire["digest"])] = wire
    ordered = tuple(descriptor_map[key] for key in sorted(descriptor_map))
    options_wire = shape_options.to_wire()
    fused_digest = fused_context_digest(
        base.context_digest,
        tuple(item["digest"] for item in ordered),
        options_wire,
    )
    return GraphContextPayload(
        identity=base.identity,
        context_digest=base.context_digest,
        material=base.material,
        fused_descriptors=ordered,
        fused_shape_options=options_wire,
        fused_digest=fused_digest,
    )


def graph_material_from_wire(value: Any) -> SnapshotMaterial:
    """Rebuild a pure :class:`SnapshotMaterial` from a graph task wire value."""

    if not isinstance(value, (tuple, list)) or len(value) != 8 or value[0] != "mc4c8-graph-material-v1":
        raise ProcessAdapterError("invalid graph material wire value")
    face_payload = tuple(value[2])
    edge_payload = tuple(value[3])
    vertex_payload = tuple(value[4])
    loop_payload = tuple(value[5])
    island_payload = tuple(value[6])
    island_face_keys = tuple(
        (tuple(item[0]), tuple(item[1])) for item in tuple(value[7])
    )
    material = SnapshotMaterial(
        mesh=tuple(value[1]),
        face_payload=face_payload,
        edge_payload=edge_payload,
        vertex_payload=vertex_payload,
        loop_payload=loop_payload,
        island_payload=island_payload,
        island_face_keys=island_face_keys,
        face_by_key=MappingProxyType({item[0]: item for item in face_payload}),
        edge_by_key=MappingProxyType({item[0]: item for item in edge_payload}),
        vertex_by_key=MappingProxyType({item[0]: item for item in vertex_payload}),
        loop_by_key=MappingProxyType({item[0]: item for item in loop_payload}),
        edge_items_by_loop=MappingProxyType({
            loop_key: tuple(item for item in edge_payload if loop_key in tuple(item[1]))
            for loop_key in {key for item in edge_payload for key in tuple(item[1])}
        }),
        vertex_items_by_loop=MappingProxyType({
            loop_key: tuple(item for item in vertex_payload if loop_key in tuple(item[1]))
            for loop_key in {key for item in vertex_payload for key in tuple(item[1])}
        }),
    )
    return material


def make_graph_build_batch(
    identity: SnapshotIdentity,
    capture: SnapshotCapture,
    island_keys: Iterable[Any],
    *,
    batch_id: str,
    debug_delay_ms: int = 0,
) -> GraphBuildTask:
    """Create a deterministic typed graph request for one or more islands."""

    items = []
    seen: set[str] = set()
    for island_key in island_keys:
        canonical_key = tuple(int(value) for value in tuple(island_key))
        key_token = repr(_payload_canonical(canonical_key))
        if key_token in seen:
            continue
        seen.add(key_token)
        material = graph_material_wire_for_island(capture, canonical_key)
        items.append(
            GraphBuildItem(
                island_key=canonical_key,
                material_digest=stable_digest(material),
                material=material,
            )
        )
    task = GraphBuildTask(
        identity=identity,
        batch_id=str(batch_id),
        graph_items=tuple(items),
        debug_delay_ms=int(debug_delay_ms),
    )
    task.validate()
    return task


def make_graph_build_context_task(
    identity: SnapshotIdentity,
    context_digest: str,
    island_keys: Iterable[Any],
    *,
    batch_id: str,
    debug_delay_ms: int = 0,
) -> GraphBuildTask:
    """Create a tiny island-only graph request for a resident context."""

    canonical_keys = tuple(sorted(
        {tuple(int(value) for value in tuple(key)) for key in island_keys},
        key=repr,
    ))
    if not canonical_keys:
        raise ProcessAdapterError("context graph task requires an island key")
    items = tuple(
        GraphBuildItem(
            island_key=key,
            material_digest=graph_context_item_digest(context_digest, key),
        )
        for key in canonical_keys
    )
    task = GraphBuildTask(
        identity=identity,
        batch_id=str(batch_id),
        graph_items=items,
        debug_delay_ms=int(debug_delay_ms),
        context_digest=str(context_digest),
    )
    task.validate()
    return task


def graph_loop_keys_for_island(
    capture: SnapshotCapture,
    island_key: Any,
) -> tuple[Any, ...]:
    """Return the already-captured loop keys without projecting GraphData."""

    material = capture.material
    if not isinstance(material, SnapshotMaterial):
        raise ProcessAdapterError("snapshot has no immutable graph material")
    face_keys = tuple(sorted(int(value) for value in tuple(island_key)))
    for declared_faces, declared_loops in material.island_face_keys:
        if tuple(sorted(int(value) for value in tuple(declared_faces))) == face_keys:
            return tuple(sorted(tuple(declared_loops), key=repr))
    raise ProcessAdapterError("snapshot island has no captured loop keys")


def make_resident_exact_batch(
    identity: SnapshotIdentity,
    context_digest: str,
    pair_specs: Iterable[
        tuple[int, Any, Any, Iterable[Any], Iterable[Any], ExactOptions]
    ],
    *,
    batch_id: str,
    debug_delay_ms: int = 0,
) -> ResidentExactBatchTask:
    """Create a small exact task backed by one worker-resident context."""

    pairs = []
    for spec in pair_specs:
        if not isinstance(spec, (tuple, list)) or len(spec) not in (6, 7, 8):
            raise ProcessAdapterError(
                "resident exact pair spec must contain six, seven or eight values"
            )
        ordinal, master_key, member_key, master_loop_keys, member_loop_keys, options = spec[:6]
        pairs.append(
            ResidentExactPair(
                pair_ordinal=int(ordinal),
                master_key=tuple(master_key),
                member_key=tuple(member_key),
                master_loop_keys=tuple(master_loop_keys),
                member_loop_keys=tuple(member_loop_keys),
                options=options,
                seed_transform=None if len(spec) == 6 else spec[6],
                correspondence_mode=(
                    CORRESPONDENCE_MODE_HYBRID if len(spec) < 8 else spec[7]
                ),
            )
        )
    pairs = tuple(pairs)
    task = ResidentExactBatchTask(
        identity=identity,
        context_digest=str(context_digest),
        batch_id=str(batch_id),
        pair_tasks=pairs,
        debug_delay_ms=int(debug_delay_ms),
    )
    task.validate()
    return task


def _fused_mode_if_explicit(value: Any) -> Optional[str]:
    """Return a normalized mode token, or None for a legacy seed value."""

    if not isinstance(value, str):
        return None
    return normalize_correspondence_mode(value)


def make_fused_batch(
    identity: SnapshotIdentity,
    context_digest: str,
    fused_digest: str,
    pair_specs: Iterable[tuple[Any, ...]],
    *,
    batch_id: str,
    debug_delay_ms: int = 0,
    correspondence_mode: Optional[str] = None,
) -> FusedBatchTask:
    """Create one master-affine task containing only immutable references.

    Nine-value specs and the historical ten-value seed-bearing specs are
    legacy HYBRID input.  Current mode-bearing specs append the normalized mode
    after the optional seed.  A caller can provide ``correspondence_mode`` to
    make the requested batch mode explicit and to reject legacy input for
    Fast/Exact instead of silently treating it as HYBRID.
    """

    pairs = []
    requested_mode = (
        None
        if correspondence_mode is None
        else normalize_correspondence_mode(correspondence_mode)
    )
    for spec in pair_specs:
        if not isinstance(spec, (tuple, list)) or len(spec) not in (9, 10, 11):
            raise ProcessAdapterError(
                "fused pair spec must contain nine legacy or ten/eleven current values"
            )
        (
            ordinal, master_key, member_key, master_descriptor_digest,
            member_descriptor_digest, master_loop_keys, member_loop_keys,
            options, prefilter
        ) = spec[:9]
        pair_mode: Optional[str] = None
        legacy_mode_less = False
        seed_transform = None
        if len(spec) == 9:
            legacy_mode_less = True
        elif len(spec) == 10:
            pair_mode = _fused_mode_if_explicit(spec[9])
            if pair_mode is None:
                legacy_mode_less = True
                seed_transform = spec[9]
        else:
            pair_mode = _fused_mode_if_explicit(spec[10])
            if pair_mode is not None:
                seed_transform = spec[9]
            else:
                pair_mode = _fused_mode_if_explicit(spec[9])
                if pair_mode is None:
                    raise ProcessAdapterError(
                        "mode-bearing fused pair spec must end with correspondence_mode"
                    )
                seed_transform = spec[10]
        effective_pair_mode = pair_mode or CORRESPONDENCE_MODE_HYBRID
        if requested_mode is not None:
            if legacy_mode_less and requested_mode != CORRESPONDENCE_MODE_HYBRID:
                raise ProcessAdapterError(
                    "legacy fused pair spec has no correspondence_mode; explicit non-HYBRID request is unsupported"
                )
            if pair_mode is not None and pair_mode != requested_mode:
                raise ProcessAdapterError(
                    "fused pair correspondence mode conflicts with requested mode"
                )
        pairs.append(
            FusedPairRef(
                pair_ordinal=int(ordinal),
                master_key=tuple(master_key),
                member_key=tuple(member_key),
                master_descriptor_digest=str(master_descriptor_digest),
                member_descriptor_digest=str(member_descriptor_digest),
                master_loop_keys=tuple(master_loop_keys),
                member_loop_keys=tuple(member_loop_keys),
                exact_options=options,
                prefilter=prefilter,
                seed_transform=seed_transform,
                correspondence_mode=effective_pair_mode,
                legacy_mode_less=legacy_mode_less,
            )
        )
    pairs = tuple(pairs)
    if pairs:
        batch_mode = requested_mode or pairs[0].correspondence_mode
        if any(pair.correspondence_mode != batch_mode for pair in pairs):
            raise ProcessAdapterError(
                "fused pair specs contain conflicting correspondence modes"
            )
    elif requested_mode is not None:
        batch_mode = requested_mode
    else:
        batch_mode = CORRESPONDENCE_MODE_HYBRID
    task = FusedBatchTask(
        identity=identity,
        context_digest=str(context_digest),
        fused_digest=str(fused_digest),
        batch_id=str(batch_id),
        pair_tasks=pairs,
        debug_delay_ms=int(debug_delay_ms),
    )
    task.validate(requested_mode=batch_mode)
    return task


def snapshot_capture_for_graph_task(
    identity: SnapshotIdentity,
    island_key: Any,
    material_wire: Any,
) -> SnapshotCapture:
    """Construct the worker-local immutable capture used by the graph builder."""

    material = graph_material_from_wire(material_wire)
    return SnapshotCapture(
        identity=identity,
        canonical=("mc4c8-worker-graph", tuple(island_key), material_wire),
        material=material,
    )


def make_exact_options(
    *,
    allow_flipping: bool,
    match_scale: bool,
    tolerance: float,
    max_search: int,
    cooperative_yield_every: int = 0,
) -> ExactOptions:
    return ExactOptions(
        allow_flipping=bool(allow_flipping),
        match_scale=bool(match_scale),
        tolerance=float(tolerance),
        max_search=int(max_search),
        cooperative_yield_every=int(cooperative_yield_every),
    )


def make_single_pair_batch(
    identity: SnapshotIdentity,
    *,
    pair_ordinal: int,
    master_key: Any,
    member_key: Any,
    master_graph: Any,
    member_graph: Any,
    options: ExactOptions,
    correspondence_mode: str = CORRESPONDENCE_MODE_HYBRID,
) -> BatchTask:
    """Create the MC3A one-pair task at the exact-ready seam."""

    master_data = graph_data_from_topology(master_graph, master_key)
    member_data = graph_data_from_topology(member_graph, member_key)
    pair = PairTask(
        pair_ordinal=int(pair_ordinal),
        master_key=tuple(master_key),
        member_key=tuple(member_key),
        master_graph=GraphRef(master_data.graph_key, master_data.content_digest),
        member_graph=GraphRef(member_data.graph_key, member_data.content_digest),
        options=options,
        correspondence_mode=correspondence_mode,
    )
    task = BatchTask(
        identity=identity,
        batch_id="mc3a-%08d" % int(pair_ordinal),
        pair_tasks=(pair,),
        graphs=tuple(
            sorted((master_data, member_data), key=lambda item: item.graph_key)
        ),
    )
    task.validate()
    return task


def _load_topology_module() -> Any:
    try:
        import topology_correspondence as topology_module  # type: ignore[no-redef]
        return topology_module
    except ImportError:
        path = Path(__file__).with_name("topology_correspondence.py")
        spec = importlib.util.spec_from_file_location("pro_process_adapter_topology", path)
        if spec is None or spec.loader is None:
            raise ProcessAdapterError("cannot load pure topology correspondence module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def pair_result_to_correspondence(
    result: PairResult,
    *,
    topology_module: Any = None,
    task: Optional[BatchTask] = None,
) -> Any:
    """Convert one validated wire result to the existing exact-result shape."""

    if not isinstance(result, PairResult):
        raise ProcessAdapterError("worker result is not a PairResult")
    if task is not None:
        pair = next(
            (
                item
                for item in task.pair_tasks
                if item.pair_ordinal == result.pair_ordinal
            ),
            None,
        )
        if pair is None:
            raise ProcessAdapterError("worker result has an unknown pair ordinal")
        if isinstance(task, (ResidentExactBatchTask, FusedBatchTask)):
            result_pair = pair
            source = set(result_pair.member_loop_keys)
            target = set(result_pair.master_loop_keys)
            if result.accepted:
                if (
                    {item[0] for item in result.loop_mapping} != source
                    or {item[1] for item in result.loop_mapping} != target
                    or len(result.loop_mapping) != len(source)
                ):
                    raise ProcessAdapterError("resident result mapping is not a full bijection")
            elif result.loop_mapping:
                raise ProcessAdapterError("resident rejected result has a mapping")
        else:
            result.validate(pair, task.graph_map)
    topology = topology_module or _load_topology_module()
    diagnostics_values = dict(result.diagnostics)
    diagnostics = topology.CorrespondenceDiagnostics(
        search_count=int(diagnostics_values.get("search_count", 0)),
        complete_mappings=int(diagnostics_values.get("complete_mappings", 0)),
        pruned_count=int(diagnostics_values.get("pruned_count", 0)),
        branch_budget=int(diagnostics_values.get("branch_budget", 0)),
        candidate_count=int(diagnostics_values.get("candidate_count", 0)),
        topology_checks=int(diagnostics_values.get("topology_checks", 0)),
        yield_count=int(diagnostics_values.get("yield_count", 0)),
        refinement_rounds=int(diagnostics_values.get("refinement_rounds", 0)),
        refinement_max_rounds=int(
            diagnostics_values.get("refinement_max_rounds", 0)
        ),
        refinement_stable=bool(
            int(diagnostics_values.get("refinement_stable", 0))
        ),
        refinement_truncated=bool(
            int(diagnostics_values.get("refinement_truncated", 0))
        ),
        refinement_elapsed_us=int(
            diagnostics_values.get("refinement_elapsed_us", 0)
        ),
        refinement_pre_max_domain=int(
            diagnostics_values.get("refinement_pre_max_domain", 0)
        ),
        refinement_post_max_domain=int(
            diagnostics_values.get("refinement_post_max_domain", 0)
        ),
    )
    transform = None
    if result.transform is not None:
        transform = topology.SimilarityTransform2D(
            angle=float(result.transform.angle),
            scale=float(result.transform.scale),
            reflected=bool(result.transform.reflected),
            source_center=tuple(result.transform.source_center),
            target_center=tuple(result.transform.target_center),
        )
    score = float(result.score) if result.score is not None else float("inf")
    residual = float(result.residual) if result.residual is not None else float("inf")
    return topology.CorrespondenceResult(
        accepted=bool(result.accepted),
        loop_mapping=tuple(result.loop_mapping),
        reflected=bool(result.reflected),
        reversed=bool(result.reversed),
        cyclic_shift=int(result.cyclic_shift),
        score=score,
        residual=residual,
        reason=str(result.reason),
        transform=transform,
        diagnostics=diagnostics,
    )


__all__ = [
    "ProcessAdapterError",
    "SnapshotCapture",
    "SnapshotMaterial",
    "SnapshotSentinel",
    "IncrementalSnapshotBuilder",
    "SnapshotGuard",
    "SnapshotGraphBuilder",
    "make_snapshot_sentinel",
    "capture_snapshot",
    "graph_key_for_island",
    "graph_data_from_topology",
    "graph_material_wire_for_island",
    "graph_material_wire_for_material",
    "graph_context_wire_for_capture",
    "graph_context_material_from_wire",
    "make_graph_context_payload",
    "make_fused_context_payload",
    "graph_material_from_wire",
    "make_graph_build_batch",
    "make_graph_build_context_task",
    "graph_loop_keys_for_island",
    "make_resident_exact_batch",
    "make_fused_batch",
    "snapshot_capture_for_graph_task",
    "make_exact_options",
    "make_single_pair_batch",
    "pair_result_to_correspondence",
]
