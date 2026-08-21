"""Direct-file-path entrypoint for one persistent pure proof worker.

This file intentionally imports its sibling protocol by file-directory path.
It is not launched as a package module, so package loader startup is avoided.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
import sys
import time
from typing import Any, Iterable


VERIFIED_NEAREST_MAX_NODES = 4096

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from pro_process_protocol import (  # noqa: E402
    Envelope,
    MAX_ERROR_TEXT_BYTES,
    MessageType,
    PROTOCOL_VERSION,
    ProtocolEOF,
    ProtocolError,
    read_message,
    write_message,
)
from pro_process_payload import (  # noqa: E402
    BatchResult,
    BatchTask,
    CORRESPONDENCE_MODE_EXACT_ONLY,
    CORRESPONDENCE_MODE_HYBRID,
    CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY,
    GraphBuildItem,
    GraphBuildEntry,
    GraphBuildResult,
    GraphBuildTask,
    GraphContextLoadResult,
    GraphContextPayload,
    GraphRef,
    PairResult,
    PairTask,
    ResidentExactBatchResult,
    ResidentExactBatchTask,
    ResidentExactPair,
    graph_context_item_digest,
    normalize_correspondence_mode,
    stable_digest,
)
from pro_process_shape import (  # noqa: E402
    FusedBatchResult,
    FusedBatchTask,
    FusedPairOutcome,
    SHAPE_OPERATION,
    ShapeBatchResult,
    ShapeBatchTask,
    ShapeDescriptor,
    ShapePairResult,
    ShapeOptions,
    _load_similarity_module,
)
import pro_process_adapter as _adapter  # noqa: E402
import pro_process_payload as _payload  # noqa: E402
import topology_correspondence as _topology  # noqa: E402
import pro_verified_nearest as _verified_nearest  # noqa: E402


THREAD_CAP_NAMES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def _bounded_text(value: object) -> str:
    text = str(value).replace("\x00", " ")
    return text[:MAX_ERROR_TEXT_BYTES]


def _thread_cap_report() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in THREAD_CAP_NAMES}


def _ready_payload() -> dict[str, Any]:
    version = sys.version_info
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operations": (
            "sum_squares",
        "exact_correspondence_batch",
        "resident_exact_correspondence_batch",
        "fused_correspondence_batch",
        SHAPE_OPERATION,
        "snapshot_graph_build_batch",
        "snapshot_graph_context_load",
        ),
        "python_version": f"{version.major}.{version.minor}.{version.micro}",
        "pid": os.getpid(),
        "executable": str(Path(sys.executable).resolve()),
        "thread_caps": _thread_cap_report(),
    }


def _normalise_items(raw_items: object) -> list[tuple[str, tuple[float, ...]]]:
    if isinstance(raw_items, dict):
        source: Iterable[object] = raw_items.items()
    elif isinstance(raw_items, (list, tuple)):
        source = raw_items
    else:
        raise ValueError("items must be a mapping or sequence")

    normalised: list[tuple[str, tuple[float, ...]]] = []
    for item in source:
        if isinstance(item, dict):
            if "key" not in item or "values" not in item:
                raise ValueError("item dict requires key and values")
            raw_key = item["key"]
            raw_values = item["values"]
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            raw_key, raw_values = item
        else:
            raise ValueError("item must be a key/value pair")
        if not isinstance(raw_values, (list, tuple)):
            raise ValueError("item values must be a sequence")
        key = str(raw_key)
        values: list[float] = []
        for raw_value in raw_values:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise ValueError("item values must be finite numbers")
            value = float(raw_value)
            if not math.isfinite(value):
                raise ValueError("item values must be finite numbers")
            values.append(value)
        normalised.append((key, tuple(values)))
    normalised.sort(key=lambda item: item[0])
    return normalised


def compute_deterministic_batch(payload: object) -> dict[str, Any]:
    """Compute a small deterministic keyed numeric proof operation."""

    if not isinstance(payload, dict):
        raise ValueError("task payload must be a mapping")
    operation = payload.get("operation", "sum_squares")
    if operation != "sum_squares":
        raise ValueError("unsupported proof operation")
    raw_offset = payload.get("offset", 0.0)
    if isinstance(raw_offset, bool) or not isinstance(raw_offset, (int, float)):
        raise ValueError("offset must be numeric")
    offset = float(raw_offset)
    if not math.isfinite(offset):
        raise ValueError("offset must be finite")
    items = _normalise_items(payload.get("items", ()))
    result_items = tuple(
        (key, sum(value * value for value in values) + offset)
        for key, values in items
    )
    return {
        "operation": operation,
        "items": result_items,
        "count": len(result_items),
    }


@dataclass
class _WorkerState:
    session_nonce: str = ""
    generation: int = 0
    ready: bool = False
    cancelled_batches: set[str] = field(default_factory=set)
    graph_cache: dict[tuple[Any, ...], Any] = field(default_factory=dict)
    graph_build_cache: dict[tuple[Any, ...], GraphBuildEntry] = field(default_factory=dict)
    shape_cache: dict[str, Any] = field(default_factory=dict)
    shape_result_cache: dict[tuple[Any, ...], Any] = field(default_factory=dict)
    shape_content_digest_cache: dict[str, str] = field(default_factory=dict)
    graph_context_payload: GraphContextPayload | None = None
    graph_context_material: Any = None
    graph_context_digest: str = ""
    fused_context_digest: str = ""
    fused_descriptors: dict[str, ShapeDescriptor] = field(default_factory=dict)
    fused_shape_options: ShapeOptions | None = None


def _clear_caches(state: _WorkerState, *, clear_context: bool = True) -> None:
    state.graph_cache.clear()
    state.graph_build_cache.clear()
    state.shape_cache.clear()
    state.shape_result_cache.clear()
    state.shape_content_digest_cache.clear()
    if clear_context:
        state.graph_context_payload = None
        state.graph_context_material = None
        state.graph_context_digest = ""
        state.fused_context_digest = ""
        state.fused_descriptors.clear()
        state.fused_shape_options = None


def _send_error(
    state: _WorkerState,
    code: str,
    message: object,
    *,
    batch_id: str = "",
    sequence: int = 0,
    item_count: int = 0,
) -> None:
    if not state.session_nonce:
        return
    try:
        write_message(
            sys.stdout.buffer,
            MessageType.ERROR,
            {
                "code": _bounded_text(code),
                "message": _bounded_text(message),
            },
            session_nonce=state.session_nonce,
            generation=state.generation,
            batch_id=batch_id,
            sequence=sequence,
            item_count=item_count,
        )
    except Exception:
        # Once stdout is unsafe, the only safe action is process exit.
        return


def _same_identity(state: _WorkerState, message: Envelope) -> bool:
    if message.session_nonce != state.session_nonce:
        _clear_caches(state)
        _send_error(state, "foreign_session", "session nonce mismatch", batch_id=message.batch_id, sequence=message.sequence)
        return False
    if message.generation != state.generation:
        _clear_caches(state)
        _send_error(state, "generation_mismatch", "generation mismatch", batch_id=message.batch_id, sequence=message.sequence)
        return False
    return True


def _cached_graph(
    state: _WorkerState,
    task: Any,
    graph_data: Any,
    *,
    validate: bool = False,
    metrics: dict[str, float] | None = None,
) -> Any:
    """Materialize one complete immutable topology graph per worker key.

    GraphData is the wire/immutable representation; IslandGraph is the pure
    solver representation.  Production callers pass ``validate=True`` so the
    latter enters the worker-local cache only after full topology validation;
    the optional flag is retained for lightweight compatibility doubles.
    ``metrics`` is deliberately local to one resident batch so diagnostics
    cannot leak into semantic digests or depend on task completion order.
    """

    identity = task.identity
    context_digest = str(getattr(task, "context_digest", "") or "")
    cache_key = (
        identity.session_nonce,
        identity.generation,
        getattr(identity, "schema_version", ""),
        getattr(identity, "algorithm_version", ""),
        identity.snapshot_digest,
        context_digest,
        graph_data.graph_key,
        graph_data.content_digest,
    )
    graph = state.graph_cache.get(cache_key)
    if graph is not None:
        if metrics is not None:
            metrics["hits"] = metrics.get("hits", 0.0) + 1.0
        return graph
    started = time.perf_counter()
    graph = graph_data.to_topology_graph(_topology)
    if validate:
        _topology._validate_graph(graph)
    # Do not retain a graph until conversion and requested full verification
    # have both completed successfully.
    state.graph_cache[cache_key] = graph
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if metrics is not None:
        metrics["builds"] = metrics.get("builds", 0.0) + 1.0
        metrics["compute_ms"] = metrics.get("compute_ms", 0.0) + elapsed_ms
    return graph


def _verified_exact_correspondence(
    master: Any,
    member: Any,
    options: Any,
    *,
    seed_transform: Any = None,
) -> tuple[Any, Any]:
    """Attempt the pure fast proof, then invoke unchanged exact search once."""

    nearest = _verified_nearest.find_verified_nearest(
        master,
        member,
        seed_transform=seed_transform,
        allow_flipping=options.allow_flipping,
        match_scale=options.match_scale,
        tolerance=options.tolerance,
        max_search=options.max_search,
        nearest_max_nodes=VERIFIED_NEAREST_MAX_NODES,
    )
    if bool(getattr(nearest, "accepted", False)):
        return nearest, nearest
    # A fast miss is deliberately not a semantic rejection.  This is the one
    # and only exact fallback invocation for this member.
    exact = _topology.find_correspondence(
        master,
        member,
        allow_flipping=options.allow_flipping,
        match_scale=options.match_scale,
        tolerance=options.tolerance,
        max_search=options.max_search,
        cooperative_yield_every=options.cooperative_yield_every,
    )
    return exact, nearest


def _correspondence_for_mode(
    master: Any,
    member: Any,
    options: Any,
    correspondence_mode: str,
    *,
    seed_transform: Any = None,
) -> tuple[Any, Any]:
    """Run exactly the algorithm selected by the immutable pair payload."""

    mode = normalize_correspondence_mode(correspondence_mode)
    if mode == CORRESPONDENCE_MODE_EXACT_ONLY:
        exact = _topology.find_correspondence(
            master,
            member,
            allow_flipping=options.allow_flipping,
            match_scale=options.match_scale,
            tolerance=options.tolerance,
            max_search=options.max_search,
            cooperative_yield_every=options.cooperative_yield_every,
        )
        return exact, None
    if mode == CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY:
        nearest = _verified_nearest.find_verified_nearest(
            master,
            member,
            seed_transform=seed_transform,
            allow_flipping=options.allow_flipping,
            match_scale=options.match_scale,
            tolerance=options.tolerance,
            max_search=options.max_search,
            nearest_max_nodes=VERIFIED_NEAREST_MAX_NODES,
        )
        return nearest, nearest
    if mode == CORRESPONDENCE_MODE_HYBRID:
        return _verified_exact_correspondence(
            master,
            member,
            options,
            seed_transform=seed_transform,
        )
    raise ValueError("unsupported correspondence mode: %s" % mode)


def _compute_exact_batch(state: _WorkerState, task: BatchTask) -> BatchResult:
    if task.identity.session_nonce != state.session_nonce or task.identity.generation != state.generation:
        _clear_caches(state)
        raise ValueError("exact task identity does not match worker session")
    task.validate()
    graph_map = task.graph_map
    pair_results: list[PairResult] = []
    for pair in task.pair_tasks:
        master_data = graph_map[pair.master_graph.graph_key]
        member_data = graph_map[pair.member_graph.graph_key]
        master = _cached_graph(state, task, master_data, validate=True)
        member = _cached_graph(state, task, member_data, validate=True)
        correspondence, nearest = _correspondence_for_mode(
            master,
            member,
            pair.options,
            pair.correspondence_mode,
        )
        pair_results.append(
            PairResult.from_correspondence(
                pair,
                correspondence,
                nearest_result=nearest,
            )
        )
    result = BatchResult(
        identity=task.identity,
        batch_id=task.batch_id,
        payload_digest=task.payload_digest(),
        pair_results=tuple(pair_results),
        complete=True,
    )
    result.validate_against(task)
    return result


def _compute_shape_batch(state: _WorkerState, task: ShapeBatchTask) -> ShapeBatchResult:
    """Run the pure descriptor matcher for one complete canonical batch."""

    if task.identity.session_nonce != state.session_nonce or task.identity.generation != state.generation:
        _clear_caches(state)
        raise ValueError("shape task identity does not match worker session")
    task.validate()
    matcher = _load_similarity_module()
    descriptor_map: dict[str, Any] = {}
    required_digests = {
        digest
        for pair in task.pair_tasks
        if pair.prefilter is None
        for digest in (pair.master_descriptor_digest, pair.member_descriptor_digest)
    }
    for descriptor in task.descriptors:
        if descriptor.descriptor_digest not in required_digests:
            continue
        if descriptor.descriptor_digest not in state.shape_content_digest_cache:
            # face_key identifies the live island, but it is not an input to
            # the pure numeric match.  Excluding it lets repeated immutable
            # UV geometry share a complete result without sharing any live
            # Blender identity.
            state.shape_content_digest_cache[descriptor.descriptor_digest] = stable_digest(
                descriptor._content_wire()[1:]
            )
        cached = state.shape_cache.get(descriptor.descriptor_digest)
        if cached is None:
            cached = descriptor.to_similarity(matcher)
            state.shape_cache[descriptor.descriptor_digest] = cached
        descriptor_map[descriptor.descriptor_digest] = cached

    pair_results: list[ShapePairResult] = []
    for pair in task.pair_tasks:
        if pair.prefilter is not None:
            pair_results.append(ShapePairResult.from_prefilter(pair, pair.prefilter))
            continue
        result_cache_key = (
            task.identity.generation,
            task.identity.snapshot_digest,
            state.shape_content_digest_cache.get(
                pair.master_descriptor_digest,
                pair.master_descriptor_digest,
            ),
            state.shape_content_digest_cache.get(
                pair.member_descriptor_digest,
                pair.member_descriptor_digest,
            ),
            pair.options.to_wire(),
        )
        cached_match = state.shape_result_cache.get(result_cache_key)
        if cached_match is not None:
            pair_results.append(ShapePairResult.from_similarity(pair, cached_match))
            continue
        diagnostics = matcher.MatcherDiagnostics()
        match = matcher.match_descriptors(
            descriptor_map[pair.master_descriptor_digest],
            descriptor_map[pair.member_descriptor_digest],
            match_scale=pair.options.match_scale,
            allow_flipping=pair.options.allow_flipping,
            tolerance=pair.options.tolerance,
            use_numpy=pair.options.use_numpy,
            diagnostics=diagnostics,
            allow_tolerant_topology=pair.options.allow_tolerant_topology,
            count_candidate=False,
        )
        # MatchResult and its DiagnosticsSnapshot are immutable; keep only
        # complete values for duplicate descriptor pairs in this generation.
        state.shape_result_cache[result_cache_key] = match
        pair_results.append(ShapePairResult.from_similarity(pair, match))
    result = ShapeBatchResult(
        identity=task.identity,
        batch_id=task.batch_id,
        payload_digest=task.payload_digest(),
        pair_results=tuple(pair_results),
        complete=True,
    )
    result.validate_against(task)
    return result


def _compute_graph_batch(
    state: _WorkerState,
    task: GraphBuildTask,
    *,
    topology_metrics: dict[str, float] | None = None,
) -> GraphBuildResult:
    """Build complete immutable SnapshotGraph values outside Blender."""

    if task.identity.session_nonce != state.session_nonce or task.identity.generation != state.generation:
        _clear_caches(state)
        raise ValueError("graph task identity does not match worker session")
    task.validate()
    if task.context_digest:
        if (
            state.graph_context_payload is None
            or state.graph_context_material is None
            or state.graph_context_digest != task.context_digest
            or state.graph_context_payload.identity != task.identity
        ):
            raise ValueError("graph task requires a loaded matching context")
    started = time.perf_counter()
    entries: list[GraphBuildEntry] = []
    cache_hits = 0
    topology_rejection_reasons = {
        "boundary_component_branch_or_open",
        "boundary_component_degenerate_segment",
        "boundary_component_not_closed",
        "boundary_component_trace_failed",
        "boundary_component_trace_not_closed",
        "boundary_component_degenerate_area",
    }
    for item in task.graph_items:
        cache_key = (
            task.identity.session_nonce,
            task.identity.generation,
            task.identity.snapshot_digest,
            stable_digest(item.island_key),
            item.material_digest,
            task.context_digest,
        )
        cached = state.graph_build_cache.get(cache_key)
        if cached is not None:
            cache_hits += 1
            entries.append(cached)
            continue
        try:
            if task.context_digest:
                expected_digest = graph_context_item_digest(
                    task.context_digest,
                    item.island_key,
                )
                if item.material is not None or item.material_digest != expected_digest:
                    raise ValueError("graph context item identity mismatch")
                material_value = _adapter.graph_material_wire_for_material(
                    state.graph_context_material,
                    item.island_key,
                )
            else:
                material_value = item.material
            declared = tuple(material_value[7]) if isinstance(material_value, (tuple, list)) and len(material_value) == 8 else ()
            declared_loops = None
            for declared_faces, loop_keys in declared:
                if tuple(sorted(int(value) for value in tuple(declared_faces))) == tuple(sorted(int(value) for value in tuple(item.island_key))):
                    declared_loops = set(loop_keys)
                    break
            face_payload = tuple(material_value[2]) if isinstance(material_value, (tuple, list)) and len(material_value) == 8 else ()
            face_keys = {int(value) for value in tuple(item.island_key)}
            partial_face = False
            for face_key, face_loop_keys, _selected in face_payload:
                if declared_loops is not None and int(face_key) in face_keys and not set(face_loop_keys).issubset(declared_loops):
                    partial_face = True
                    break
            if partial_face:
                entry = GraphBuildEntry(
                    island_key=item.island_key,
                    material_digest=item.material_digest,
                    accepted=False,
                    reason="invalid_record_partial_face",
                    complete=True,
                )
                state.graph_build_cache[cache_key] = entry
                entries.append(entry)
                continue
            capture = _adapter.snapshot_capture_for_graph_task(
                task.identity,
                item.island_key,
                material_value,
            )
            builder = _adapter.SnapshotGraphBuilder(capture, item.island_key, {})
            while not builder.done:
                builder.advance(operation_budget=128)
            graph_result = builder.result
            if graph_result is None:
                raise _adapter.ProcessAdapterError("graph builder returned no result")
            graph_data, _unused_live = graph_result
            graph_data.validate()
            try:
                _cached_graph(
                    state,
                    task,
                    graph_data,
                    validate=True,
                    metrics=topology_metrics,
                )
            except Exception as exc:
                reason = str(getattr(exc, "reason", exc)).split(":", 1)[0].strip()
                if not (reason.startswith("invalid_record_") or reason == "non_manifold_topology"):
                    raise
                entry = GraphBuildEntry(
                    island_key=item.island_key,
                    material_digest=item.material_digest,
                    accepted=False,
                    reason=reason,
                    complete=True,
                )
                state.graph_build_cache[cache_key] = entry
                entries.append(entry)
                continue
            entry = GraphBuildEntry(
                island_key=item.island_key,
                material_digest=item.material_digest,
                accepted=True,
                graph=graph_data,
                reason="",
                complete=True,
            )
        except _adapter.ProcessAdapterError as exc:
            # Only boundary failures that the legacy topology path treats as
            # a deterministic topology rejection may become a complete
            # negative result.  Adapter/schema/material failures must remain
            # worker errors so the pool can retry or cancel the operation.
            reason = str(exc).split(":", 1)[0].strip() or type(exc).__name__
            if reason not in topology_rejection_reasons:
                raise
            entry = GraphBuildEntry(
                island_key=item.island_key,
                material_digest=item.material_digest,
                accepted=False,
                graph=None,
                reason=reason,
                complete=True,
            )
        state.graph_build_cache[cache_key] = entry
        entries.append(entry)
    result = GraphBuildResult(
        identity=task.identity,
        batch_id=task.batch_id,
        payload_digest=task.payload_digest(),
        graph_results=tuple(entries),
        complete=True,
        compute_ms=(time.perf_counter() - started) * 1000.0,
        cache_hits=cache_hits,
    )
    result.validate_against(task)
    return result


def _resident_entry_digest(context_digest: str, island_key: Any) -> str:
    return graph_context_item_digest(context_digest, island_key)


def _resident_loop_identity(entry: GraphBuildEntry, pair: ResidentExactPair, *, master: bool) -> None:
    if not entry.accepted or entry.graph is None:
        return
    expected = pair.master_loop_keys if master else pair.member_loop_keys
    actual = tuple(sorted((item.key for item in entry.graph.loops), key=repr))
    if actual != tuple(sorted(expected, key=repr)):
        role = "master" if master else "member"
        raise ValueError(f"resident {role} loop identity mismatch")


def _compute_resident_exact_batch(
    state: _WorkerState,
    task: ResidentExactBatchTask,
) -> ResidentExactBatchResult:
    """Build/cache graphs and run exact correspondence without wire GraphData."""

    if task.identity.session_nonce != state.session_nonce or task.identity.generation != state.generation:
        _clear_caches(state)
        raise ValueError("resident exact task identity does not match worker session")
    if (
        state.graph_context_payload is None
        or state.graph_context_material is None
        or state.graph_context_digest != task.context_digest
        or state.graph_context_payload.identity != task.identity
    ):
        raise ValueError("resident exact task requires a loaded matching context")
    task.validate()
    graph_items: list[Any] = []
    seen: dict[str, tuple[Any, ...]] = {}
    for pair in task.pair_tasks:
        for island_key in (pair.master_key, pair.member_key):
            key_token = stable_digest(island_key)
            if key_token in seen:
                continue
            seen[key_token] = tuple(island_key)
            graph_items.append(
                GraphBuildItem(
                    island_key=tuple(island_key),
                    material_digest=_resident_entry_digest(task.context_digest, island_key),
                )
            )
    graph_task = GraphBuildTask(
        identity=task.identity,
        batch_id=f"{task.batch_id}-graphs",
        graph_items=tuple(graph_items),
        debug_delay_ms=task.debug_delay_ms,
        context_digest=task.context_digest,
    )
    topology_metrics: dict[str, float] = {
        "builds": 0.0,
        "hits": 0.0,
        "compute_ms": 0.0,
    }
    graph_result = _compute_graph_batch(
        state,
        graph_task,
        topology_metrics=topology_metrics,
    )
    entry_map = {tuple(entry.island_key): entry for entry in graph_result.graph_results}
    exact_started = time.perf_counter()
    pair_results: list[PairResult] = []
    for pair in task.pair_tasks:
        master_entry = entry_map[tuple(pair.master_key)]
        member_entry = entry_map[tuple(pair.member_key)]
        _resident_loop_identity(master_entry, pair, master=True)
        _resident_loop_identity(member_entry, pair, master=False)
        if not master_entry.accepted or not member_entry.accepted:
            rejected = master_entry if not master_entry.accepted else member_entry
            reason = str(rejected.reason) or "resident_graph_rejected"
            pair_results.append(
                PairResult(
                    pair_ordinal=pair.pair_ordinal,
                    master_key=pair.master_key,
                    member_key=pair.member_key,
                    master_graph_digest=(
                        master_entry.graph_digest
                        or _resident_entry_digest(task.context_digest, pair.master_key)
                    ),
                    member_graph_digest=(
                        member_entry.graph_digest
                        or _resident_entry_digest(task.context_digest, pair.member_key)
                    ),
                    accepted=False,
                    reason=reason,
                    diagnostics=(
                        ("graph_rejected_before_nearest", 1),
                        ("nearest_seed_missing", 0),
                        ("nearest_attempted", 0),
                        ("nearest_accepted", 0),
                        ("nearest_fast_miss", 0),
                        ("exact_fallback_calls", 0),
                        ("exact_primary_calls", 0),
                    ),
                    complete=True,
                    correspondence_mode=pair.correspondence_mode,
                )
            )
            continue
        if master_entry.graph is None or member_entry.graph is None:
            raise ValueError("resident accepted graph is missing")
        pair_task = PairTask(
            pair_ordinal=pair.pair_ordinal,
            master_key=pair.master_key,
            member_key=pair.member_key,
            master_graph=GraphRef(
                master_entry.graph.graph_key,
                master_entry.graph.content_digest,
            ),
            member_graph=GraphRef(
                member_entry.graph.graph_key,
                member_entry.graph.content_digest,
            ),
            options=pair.options,
            correspondence_mode=pair.correspondence_mode,
        )
        master = _cached_graph(
            state,
            task,
            master_entry.graph,
            validate=True,
            metrics=topology_metrics,
        )
        member = _cached_graph(
            state,
            task,
            member_entry.graph,
            validate=True,
            metrics=topology_metrics,
        )
        correspondence, nearest = _correspondence_for_mode(
            master,
            member,
            pair.options,
            pair.correspondence_mode,
            seed_transform=pair.seed_transform,
        )
        pair_results.append(
            PairResult.from_correspondence(
                pair_task,
                correspondence,
                nearest_result=nearest,
            )
        )
    result = ResidentExactBatchResult(
        identity=task.identity,
        context_digest=task.context_digest,
        batch_id=task.batch_id,
        payload_digest=task.payload_digest(),
        pair_results=tuple(pair_results),
        complete=True,
        graph_cache_builds=max(0, len(graph_result.graph_results) - graph_result.cache_hits),
        graph_cache_hits=graph_result.cache_hits,
        graph_compute_ms=graph_result.compute_ms,
        exact_compute_ms=(time.perf_counter() - exact_started) * 1000.0,
        topology_cache_builds=int(topology_metrics["builds"]),
        topology_cache_hits=int(topology_metrics["hits"]),
        topology_compute_ms=float(topology_metrics["compute_ms"]),
    )
    result.validate_against(task)
    return result


def _compute_fused_batch(
    state: _WorkerState,
    task: FusedBatchTask,
) -> FusedBatchResult:
    """Run shape, resident graph build/cache and exact in one worker call."""

    if task.identity.session_nonce != state.session_nonce or task.identity.generation != state.generation:
        _clear_caches(state)
        raise ValueError("fused task identity does not match worker session")
    if (
        state.graph_context_payload is None
        or state.graph_context_material is None
        or state.graph_context_digest != task.context_digest
        or state.fused_context_digest != task.fused_digest
        or state.graph_context_payload.identity != task.identity
        or state.fused_shape_options is None
    ):
        raise ValueError("fused task requires a loaded matching fused context")
    task.validate()
    matcher = _load_similarity_module()
    descriptor_map = state.fused_descriptors
    shape_started = time.perf_counter()
    shape_results: dict[int, ShapePairResult] = {}
    shape_cache_hits = 0
    required_digests = {
        digest
        for pair in task.pair_tasks
        if pair.prefilter is None
        for digest in (pair.master_descriptor_digest, pair.member_descriptor_digest)
    }
    similarity_map: dict[str, Any] = {}
    for digest in sorted(required_digests):
        descriptor = descriptor_map.get(digest)
        if descriptor is None:
            raise ValueError("fused task references an unknown descriptor")
        cached = state.shape_cache.get(digest)
        if cached is None:
            cached = descriptor.to_similarity(matcher)
            state.shape_cache[digest] = cached
        else:
            shape_cache_hits += 1
        similarity_map[digest] = cached

    for pair in task.pair_tasks:
        shape_pair = pair.to_shape_pair(state.fused_shape_options)
        if pair.prefilter is not None:
            shape_results[pair.pair_ordinal] = ShapePairResult.from_prefilter(
                shape_pair, pair.prefilter
            )
            continue
        result_cache_key = (
            task.identity.generation,
            task.identity.snapshot_digest,
            pair.master_descriptor_digest,
            pair.member_descriptor_digest,
            state.fused_shape_options.to_wire(),
        )
        cached_match = state.shape_result_cache.get(result_cache_key)
        if cached_match is not None:
            shape_cache_hits += 1
            match = cached_match
        else:
            diagnostics = matcher.MatcherDiagnostics()
            match = matcher.match_descriptors(
                similarity_map[pair.master_descriptor_digest],
                similarity_map[pair.member_descriptor_digest],
                match_scale=state.fused_shape_options.match_scale,
                allow_flipping=state.fused_shape_options.allow_flipping,
                tolerance=state.fused_shape_options.tolerance,
                use_numpy=state.fused_shape_options.use_numpy,
                diagnostics=diagnostics,
                allow_tolerant_topology=state.fused_shape_options.allow_tolerant_topology,
                count_candidate=False,
            )
            state.shape_result_cache[result_cache_key] = match
        shape_results[pair.pair_ordinal] = ShapePairResult.from_similarity(
            shape_pair, match
        )
    shape_compute_ms = (time.perf_counter() - shape_started) * 1000.0

    accepted_shape_pairs = tuple(
        pair for pair in task.pair_tasks
        if shape_results[pair.pair_ordinal].accepted
    )
    # R2D's descriptor SSE is computed over resampled boundary points.  The
    # current immutable payload does not prove that those samples are a
    # subset of the raw graph loops consumed by CorrespondenceSearch, so the
    # scalar is observational only in R2E1.  Never use it to reject a live
    # pair.  Keep the counters explicit so old reports remain schema-safe.
    lower_bound_checked = 0
    lower_bound_rejected = 0
    lower_bound_skipped = len(accepted_shape_pairs)
    lower_bound_graph_pairs_avoided = 0
    lower_bound_ratios: list[float] = []
    accepted_pairs = accepted_shape_pairs
    graph_entries: dict[tuple[Any, ...], GraphBuildEntry] = {}
    graph_cache_builds = 0
    graph_cache_hits = 0
    graph_compute_ms = 0.0
    if accepted_pairs:
        graph_items: list[GraphBuildItem] = []
        seen_keys: set[str] = set()
        for pair in accepted_pairs:
            for island_key in (pair.master_key, pair.member_key):
                token = stable_digest(island_key)
                if token in seen_keys:
                    continue
                seen_keys.add(token)
                graph_items.append(
                    GraphBuildItem(
                        island_key=tuple(island_key),
                        material_digest=graph_context_item_digest(
                            task.context_digest, island_key
                        ),
                    )
                )
        graph_task = GraphBuildTask(
            identity=task.identity,
            batch_id=f"{task.batch_id}-graphs",
            graph_items=tuple(graph_items),
            debug_delay_ms=task.debug_delay_ms,
            context_digest=task.context_digest,
        )
        graph_result = _compute_graph_batch(state, graph_task)
        graph_cache_hits = int(graph_result.cache_hits)
        graph_cache_builds = max(0, len(graph_result.graph_results) - graph_cache_hits)
        graph_compute_ms = float(graph_result.compute_ms)
        graph_entries = {
            tuple(entry.island_key): entry for entry in graph_result.graph_results
        }

    exact_started = time.perf_counter()
    outcomes: list[FusedPairOutcome] = []
    for pair in task.pair_tasks:
        shape_result = shape_results[pair.pair_ordinal]
        if not shape_result.accepted:
            outcomes.append(
                FusedPairOutcome(
                    pair_ordinal=pair.pair_ordinal,
                    shape_result=shape_result,
                    exact_result=None,
                    terminal_reason=shape_result.reason,
                    correspondence_mode=pair.correspondence_mode,
                )
            )
            continue
        master_entry = graph_entries[tuple(pair.master_key)]
        member_entry = graph_entries[tuple(pair.member_key)]
        if not master_entry.accepted or not member_entry.accepted:
            rejected = master_entry if not master_entry.accepted else member_entry
            exact_result = PairResult(
                pair_ordinal=pair.pair_ordinal,
                master_key=pair.master_key,
                member_key=pair.member_key,
                master_graph_digest=(
                    rejected.graph_digest
                    or graph_context_item_digest(task.context_digest, pair.master_key)
                ),
                member_graph_digest=(
                    member_entry.graph_digest
                    or graph_context_item_digest(task.context_digest, pair.member_key)
                ),
                accepted=False,
                reason=str(rejected.reason) or "fused_graph_rejected",
                diagnostics=(
                    ("graph_rejected_before_nearest", 1),
                    ("nearest_seed_missing", 0),
                    ("nearest_attempted", 0),
                    ("nearest_accepted", 0),
                    ("nearest_fast_miss", 0),
                    ("exact_fallback_calls", 0),
                    ("exact_primary_calls", 0),
                ),
                complete=True,
                correspondence_mode=pair.correspondence_mode,
            )
            outcomes.append(
                FusedPairOutcome(
                    pair_ordinal=pair.pair_ordinal,
                    shape_result=shape_result,
                    exact_result=exact_result,
                    terminal_reason=exact_result.reason,
                    correspondence_mode=pair.correspondence_mode,
                )
            )
            continue
        if master_entry.graph is None or member_entry.graph is None:
            raise ValueError("fused accepted graph has no GraphData")
        pair_task = PairTask(
            pair_ordinal=pair.pair_ordinal,
            master_key=pair.master_key,
            member_key=pair.member_key,
            master_graph=GraphRef(
                master_entry.graph.graph_key,
                master_entry.graph.content_digest,
            ),
            member_graph=GraphRef(
                member_entry.graph.graph_key,
                member_entry.graph.content_digest,
            ),
            options=pair.exact_options,
            correspondence_mode=pair.correspondence_mode,
        )
        master = _cached_graph(state, task, master_entry.graph, validate=True)
        member = _cached_graph(state, task, member_entry.graph, validate=True)
        correspondence, nearest = _correspondence_for_mode(
            master,
            member,
            pair.exact_options,
            pair.correspondence_mode,
            seed_transform=pair.seed_transform,
        )
        exact_result = PairResult.from_correspondence(
            pair_task,
            correspondence,
            nearest_result=nearest,
        )
        outcomes.append(
            FusedPairOutcome(
                pair_ordinal=pair.pair_ordinal,
                shape_result=shape_result,
                exact_result=exact_result,
                terminal_reason=exact_result.reason,
                correspondence_mode=pair.correspondence_mode,
            )
        )

    result = FusedBatchResult(
        identity=task.identity,
        context_digest=task.context_digest,
        fused_digest=task.fused_digest,
        batch_id=task.batch_id,
        payload_digest=task.payload_digest(),
        outcomes=tuple(outcomes),
        complete=True,
        graph_cache_builds=graph_cache_builds,
        graph_cache_hits=graph_cache_hits,
        graph_compute_ms=graph_compute_ms,
        exact_compute_ms=(time.perf_counter() - exact_started) * 1000.0,
        shape_compute_ms=shape_compute_ms,
        shape_cache_hits=shape_cache_hits,
        lower_bound_checked=lower_bound_checked,
        lower_bound_rejected=lower_bound_rejected,
        lower_bound_skipped=lower_bound_skipped,
        lower_bound_graph_pairs_avoided=lower_bound_graph_pairs_avoided,
        lower_bound_min_ratio=min(lower_bound_ratios, default=0.0),
        lower_bound_max_ratio=max(lower_bound_ratios, default=0.0),
    )
    result.validate_against(task)
    return result


def _compute_graph_context_load(
    state: _WorkerState,
    payload: GraphContextPayload,
    *,
    batch_id: str,
) -> GraphContextLoadResult:
    """Load one complete immutable topology context into this worker."""

    if payload.identity.session_nonce != state.session_nonce or payload.identity.generation != state.generation:
        _clear_caches(state)
        raise ValueError("graph context identity does not match worker session")
    started = time.perf_counter()
    material = _adapter.graph_context_material_from_wire(payload.material)
    state.graph_context_payload = payload
    state.graph_context_material = material
    state.graph_context_digest = payload.context_digest
    state.fused_descriptors.clear()
    state.fused_shape_options = None
    state.fused_context_digest = ""
    if payload.fused_descriptors or payload.fused_shape_options is not None:
        state.fused_descriptors = {
            descriptor.descriptor_digest: descriptor
            for descriptor in (
                ShapeDescriptor.from_wire(item)
                for item in payload.fused_descriptors
            )
        }
        state.fused_shape_options = ShapeOptions.from_wire(payload.fused_shape_options)
        state.fused_context_digest = payload.fused_digest
        state.shape_cache.clear()
        state.shape_result_cache.clear()
        state.shape_content_digest_cache.clear()
    # A context replacement invalidates island projections, but complete shape
    # values remain independent of topology and may be retained.
    state.graph_cache.clear()
    state.graph_build_cache.clear()
    result = GraphContextLoadResult(
        identity=payload.identity,
        batch_id=str(batch_id),
        context_digest=payload.context_digest,
        complete=True,
        load_ms=(time.perf_counter() - started) * 1000.0,
    )
    return result


def run_worker() -> int:
    state = _WorkerState()
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    while True:
        try:
            message = read_message(input_stream)
        except ProtocolEOF:
            return 0
        except ProtocolError:
            return 2
        except Exception:
            return 2

        if message.message_type is MessageType.HELLO:
            if state.ready:
                _send_error(state, "duplicate_hello", "HELLO already completed")
                return 3
            if not message.session_nonce:
                return 3
            state.session_nonce = message.session_nonce
            state.generation = message.generation
            state.ready = True
            _clear_caches(state)
            try:
                write_message(
                    output_stream,
                    MessageType.READY,
                    _ready_payload(),
                    session_nonce=state.session_nonce,
                    generation=state.generation,
                    batch_id="",
                    sequence=message.sequence,
                    item_count=0,
                )
            except Exception:
                return 3
            continue

        if not state.ready:
            return 3
        if not _same_identity(state, message):
            return 3

        if message.message_type is MessageType.TASK:
            if message.batch_id in state.cancelled_batches:
                _send_error(state, "cancelled", "task was cancelled", batch_id=message.batch_id, sequence=message.sequence)
                continue
            try:
                operation = message.payload.get("operation") if isinstance(message.payload, dict) else None
                if operation == "exact_correspondence_batch":
                    task = BatchTask.from_wire(message.payload)
                    if task.debug_delay_ms:
                        time.sleep(task.debug_delay_ms / 1000.0)
                    result = _compute_exact_batch(state, task).to_wire()
                elif operation == "resident_exact_correspondence_batch":
                    task = ResidentExactBatchTask.from_wire(message.payload)
                    if task.debug_delay_ms:
                        time.sleep(task.debug_delay_ms / 1000.0)
                    result = _compute_resident_exact_batch(state, task).to_wire()
                elif operation == "fused_correspondence_batch":
                    task = FusedBatchTask.from_wire(message.payload)
                    if task.debug_delay_ms:
                        time.sleep(task.debug_delay_ms / 1000.0)
                    result = _compute_fused_batch(state, task).to_wire()
                elif operation == "snapshot_graph_build_batch":
                    task = GraphBuildTask.from_wire(message.payload)
                    if task.debug_delay_ms:
                        time.sleep(task.debug_delay_ms / 1000.0)
                    result = _compute_graph_batch(state, task).to_wire()
                elif operation == "snapshot_graph_context_load":
                    payload = GraphContextPayload.from_wire(message.payload)
                    result = _compute_graph_context_load(
                        state,
                        payload,
                        batch_id=message.batch_id,
                    ).to_wire()
                elif operation == SHAPE_OPERATION:
                    task = ShapeBatchTask.from_wire(message.payload)
                    if task.debug_delay_ms:
                        time.sleep(task.debug_delay_ms / 1000.0)
                    result = _compute_shape_batch(state, task).to_wire()
                else:
                    result = compute_deterministic_batch(message.payload)
            except Exception as exc:
                _send_error(
                    state,
                    "task_error",
                    exc,
                    batch_id=message.batch_id,
                    sequence=message.sequence,
                    item_count=message.item_count,
                )
                continue
            try:
                write_message(
                    output_stream,
                    MessageType.RESULT,
                    result,
                    session_nonce=state.session_nonce,
                    generation=state.generation,
                    batch_id=message.batch_id,
                    sequence=message.sequence,
                    item_count=message.item_count,
                )
            except Exception:
                return 3
            continue

        if message.message_type is MessageType.CANCEL:
            state.cancelled_batches.add(message.batch_id)
            _clear_caches(state)
            try:
                write_message(
                    output_stream,
                    MessageType.CANCEL_ACK,
                    {"cancelled": message.batch_id},
                    session_nonce=state.session_nonce,
                    generation=state.generation,
                    batch_id=message.batch_id,
                    sequence=message.sequence,
                    item_count=0,
                )
            except Exception:
                return 3
            continue

        if message.message_type is MessageType.SHUTDOWN:
            _clear_caches(state)
            try:
                write_message(
                    output_stream,
                    MessageType.SHUTDOWN_ACK,
                    {"shutdown": True},
                    session_nonce=state.session_nonce,
                    generation=state.generation,
                    batch_id="",
                    sequence=message.sequence,
                    item_count=0,
                )
            except Exception:
                return 3
            return 0

        _send_error(state, "unexpected_message", message.message_type.name, batch_id=message.batch_id, sequence=message.sequence)
        return 3


def main() -> int:
    return run_worker()


if __name__ == "__main__":
    raise SystemExit(main())
