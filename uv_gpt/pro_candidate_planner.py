"""Bounded, deterministic candidate planning for Align Similar Pro.

This module is deliberately independent from Blender.  The caller extracts
one immutable :class:`IslandRecord` per UV island on Blender's main thread and
hands those records to :class:`CandidatePlan`.  The planner only narrows the
set of pairs that a later exact topology correspondence and full fit may
prove; it never groups islands, follows transitive edges, or writes UV data.

The planner has three deliberately separate gates:

* strict topology fingerprints form the first bucket and prevent obviously
  incompatible graph records from sharing an index;
* quantized normalized descriptors provide a cheap, overlap-safe neighboring
  bin query;
* a bounded canonical descriptor probe is used only to fill a short candidate
  list when neighboring bins do not provide enough masters.

Every potentially expensive operation is bounded by ``PlannerConfig``.  A
plan stores records and an index, but never stores the emitted pair stream.
Use :meth:`CandidatePlan.iter_batches` when the caller wants bounded batches.
The final P02 runtime must still run exact full correspondence for every
emitted pair before applying anything.
"""

from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, fields, is_dataclass
from functools import cmp_to_key
import hashlib
import itertools
import math
import numbers
import sys
import time
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


DEFAULT_PER_MEMBER_K = 8
DEFAULT_GLOBAL_PAIR_BUDGET = 4096
DEFAULT_PER_BUCKET_PAIR_BUDGET = 4096
DEFAULT_DESCRIPTOR_BIN_WIDTH = 0.05
DEFAULT_INDEX_DIMENSIONS = 4
DEFAULT_FALLBACK_PROBE_LIMIT = 16
DEFAULT_FALLBACK_CANDIDATE_LIMIT = 8
DEFAULT_BATCH_SIZE = 256
DEFAULT_DENSITY_TIE_EPSILON = 1.0e-12
DEFAULT_GRAPH_REFINEMENT_ROUNDS = 8
# A face partition can gain at most one new colour class per refinement
# round.  The face-count-derived bound is therefore finite; this cap keeps a
# pathological single island from monopolising the planner.  A capped run is
# recorded as truncated in ``FaceRefinementResult`` and remains a safe
# invariant (it can only leave extra candidates in the bucket).
DEFAULT_FACE_REFINEMENT_MAX_ROUNDS = 64
MAX_FALLBACK_PROBE_LIMIT = 64

REASON_PER_MEMBER_K = "per_member_k"
REASON_BUCKET_PAIR_BUDGET = "per_bucket_pair_budget"
REASON_GLOBAL_PAIR_BUDGET = "global_pair_budget"
REASON_FALLBACK_PROBE_LIMIT = "fallback_probe_limit"
REASON_CANDIDATE_INDEX_LIMITED = "candidate_index_limited"
REASON_NO_VALID_MASTER = "no_valid_master"
REASON_NO_OTHER_MASTER = "no_other_master"
REASON_INVALID_MEMBER_DENSITY = "invalid_member_density"

_TRUNCATION_REASONS = frozenset(
    {
        REASON_PER_MEMBER_K,
        REASON_BUCKET_PAIR_BUDGET,
        REASON_GLOBAL_PAIR_BUDGET,
        REASON_FALLBACK_PROBE_LIMIT,
        REASON_CANDIDATE_INDEX_LIMITED,
    }
)


def _stable_sort_key(value: Any) -> Tuple[str, str]:
    """Return a total ordering token for frozen adapter-owned values."""

    return (type(value).__name__, repr(value))


def _compact_bucket_token(value: Any) -> Any:
    """Keep diagnostics readable without retaining a full graph fingerprint."""

    text = repr(value)
    if len(text) <= 96:
        return value
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return ("fingerprint_sha256", digest)


def _freeze(value: Any, *, path: str = "value") -> Any:
    """Recursively freeze small adapter records and reject mutable payloads."""

    if value is None or isinstance(value, (bool, str, bytes, numbers.Integral)):
        return value
    if isinstance(value, numbers.Real):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("%s contains a non-finite number" % path)
        return result
    if isinstance(value, (tuple, list)):
        return tuple(
            _freeze(item, path="%s[%d]" % (path, index))
            for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        items = [
            (
                _freeze(key, path="%s.key" % path),
                _freeze(item, path="%s.value" % path),
            )
            for key, item in value.items()
        ]
        return tuple(sorted(items, key=lambda pair: _stable_sort_key(pair[0])))
    if isinstance(value, (set, frozenset)):
        return tuple(
            sorted(
                (_freeze(item, path="%s.item" % path) for item in value),
                key=_stable_sort_key,
            )
        )
    if is_dataclass(value):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not getattr(params, "frozen", False):
            raise TypeError("%s must be a frozen dataclass" % path)
        return tuple(
            (
                field.name,
                _freeze(getattr(value, field.name), path="%s.%s" % (path, field.name)),
            )
            for field in fields(value)
        )
    raise TypeError(
        "%s must contain immutable scalar/tuple values, got %s"
        % (path, type(value).__name__)
    )


def _as_tuple(value: Any, *, path: str) -> Tuple[Any, ...]:
    frozen = _freeze(value, path=path)
    if isinstance(frozen, tuple):
        return frozen
    return (frozen,)


def _face_sort_key(face_key: Any) -> Tuple[Any, ...]:
    """Sort normal Blender face-key tuples numerically, with a safe fallback."""

    if isinstance(face_key, tuple) and all(
        isinstance(item, numbers.Integral) and not isinstance(item, bool)
        for item in face_key
    ):
        return (0, tuple(int(item) for item in face_key))
    if isinstance(face_key, tuple) and all(isinstance(item, str) for item in face_key):
        return (1, tuple(face_key))
    return (2, _stable_sort_key(face_key))


def _descriptor_key(descriptor: Sequence[float]) -> Tuple[float, ...]:
    return tuple(float(value) for value in descriptor)


def _valid_density(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        density = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(density) or density <= 0.0:
        return None
    return density


@dataclass(frozen=True, init=False)
class IslandRecord:
    """Immutable input record extracted from one UV island.

    ``strict_topology_fingerprint`` must contain a canonical graph-aware
    fingerprint, not only counts.  :func:`canonical_graph_color_signature`
    creates one from an immutable ``topology_correspondence.IslandGraph``.
    ``normalized_boundary_descriptor`` is a short numeric invariant vector;
    it is an index hint, never a final shape decision.

    ``topology_fingerprint`` and ``normalized_shape_descriptor`` are accepted
    as keyword aliases to keep the P02 adapter readable.  Invalid density is
    normalized to ``None`` so the planner can report an unresolved member
    without inventing a master.
    """

    face_key: Tuple[Any, ...]
    strict_topology_fingerprint: Any
    normalized_boundary_descriptor: Tuple[float, ...]
    density: Optional[float]
    cheap_signature: Any

    def __init__(
        self,
        face_key: Any,
        strict_topology_fingerprint: Any = None,
        normalized_boundary_descriptor: Any = (),
        density: Any = None,
        cheap_signature: Any = (),
        *,
        topology_fingerprint: Any = None,
        normalized_shape_descriptor: Any = None,
    ) -> None:
        if strict_topology_fingerprint is None:
            strict_topology_fingerprint = topology_fingerprint
        elif topology_fingerprint is not None and _freeze(
            strict_topology_fingerprint, path="strict_topology_fingerprint"
        ) != _freeze(topology_fingerprint, path="topology_fingerprint"):
            raise ValueError("conflicting topology fingerprint aliases")
        if strict_topology_fingerprint is None:
            raise ValueError("strict_topology_fingerprint is required")

        if normalized_shape_descriptor is not None:
            if normalized_boundary_descriptor not in ((), None) and _freeze(
                normalized_boundary_descriptor, path="normalized_boundary_descriptor"
            ) != _freeze(
                normalized_shape_descriptor, path="normalized_shape_descriptor"
            ):
                raise ValueError("conflicting descriptor aliases")
            if normalized_boundary_descriptor in ((), None):
                normalized_boundary_descriptor = normalized_shape_descriptor

        frozen_face_key = _as_tuple(face_key, path="face_key")
        if not frozen_face_key:
            raise ValueError("face_key must not be empty")
        frozen_fingerprint = _freeze(
            strict_topology_fingerprint, path="strict_topology_fingerprint"
        )
        frozen_descriptor = _as_tuple(
            normalized_boundary_descriptor, path="normalized_boundary_descriptor"
        )
        descriptor_values = []
        for index, item in enumerate(frozen_descriptor):
            try:
                numeric = float(item)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "normalized_boundary_descriptor[%d] is not numeric" % index
                ) from exc
            if not math.isfinite(numeric):
                raise ValueError(
                    "normalized_boundary_descriptor[%d] is not finite" % index
                )
            descriptor_values.append(numeric)

        object.__setattr__(self, "face_key", frozen_face_key)
        object.__setattr__(self, "strict_topology_fingerprint", frozen_fingerprint)
        object.__setattr__(self, "normalized_boundary_descriptor", tuple(descriptor_values))
        object.__setattr__(self, "density", _valid_density(density))
        object.__setattr__(
            self, "cheap_signature", _freeze(cheap_signature, path="cheap_signature")
        )

    @property
    def topology_fingerprint(self) -> Any:
        """Compatibility alias for the strict fingerprint field."""

        return self.strict_topology_fingerprint

    @property
    def normalized_shape_descriptor(self) -> Tuple[float, ...]:
        """Compatibility alias for the normalized boundary descriptor."""

        return self.normalized_boundary_descriptor

    @property
    def has_valid_density(self) -> bool:
        return self.density is not None


@dataclass(frozen=True)
class PlannerConfig:
    """Hard limits for one bounded candidate-planning run."""

    per_member_k: int = DEFAULT_PER_MEMBER_K
    global_pair_budget: int = DEFAULT_GLOBAL_PAIR_BUDGET
    per_bucket_pair_budget: int = DEFAULT_PER_BUCKET_PAIR_BUDGET
    descriptor_bin_width: float = DEFAULT_DESCRIPTOR_BIN_WIDTH
    index_dimensions: int = DEFAULT_INDEX_DIMENSIONS
    fallback_probe_limit: int = DEFAULT_FALLBACK_PROBE_LIMIT
    fallback_candidate_limit: int = DEFAULT_FALLBACK_CANDIDATE_LIMIT
    batch_size: int = DEFAULT_BATCH_SIZE
    density_tie_epsilon: float = DEFAULT_DENSITY_TIE_EPSILON

    def __post_init__(self) -> None:
        for name in (
            "per_member_k",
            "global_pair_budget",
            "per_bucket_pair_budget",
            "index_dimensions",
            "fallback_probe_limit",
            "fallback_candidate_limit",
            "batch_size",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("%s must be a non-negative integer" % name)
        if self.index_dimensions > 6:
            raise ValueError("index_dimensions must be <= 6 for bounded neighbor enumeration")
        if self.fallback_probe_limit > MAX_FALLBACK_PROBE_LIMIT:
            raise ValueError(
                "fallback_probe_limit must be <= %d for bounded lookup"
                % MAX_FALLBACK_PROBE_LIMIT
            )
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        try:
            width = float(self.descriptor_bin_width)
        except (TypeError, ValueError) as exc:
            raise ValueError("descriptor_bin_width must be positive") from exc
        if not math.isfinite(width) or width <= 0.0:
            raise ValueError("descriptor_bin_width must be positive")
        object.__setattr__(self, "descriptor_bin_width", width)
        try:
            epsilon = float(self.density_tie_epsilon)
        except (TypeError, ValueError) as exc:
            raise ValueError("density_tie_epsilon must be non-negative") from exc
        if not math.isfinite(epsilon) or epsilon < 0.0:
            raise ValueError("density_tie_epsilon must be non-negative")
        object.__setattr__(self, "density_tie_epsilon", epsilon)


CandidatePlannerConfig = PlannerConfig


@dataclass(frozen=True)
class CandidatePair:
    """One direct member-to-master candidate emitted by the planner."""

    member_key: Tuple[Any, ...]
    master_key: Tuple[Any, ...]
    rank: int
    tier: str
    descriptor_distance: float
    master_density: Optional[float]

    @property
    def candidate_key(self) -> Tuple[Any, ...]:
        """Alias used by adapters that call the member the candidate."""

        return self.member_key

    @property
    def key(self) -> Tuple[Tuple[Any, ...], Tuple[Any, ...]]:
        return self.member_key, self.master_key


@dataclass(frozen=True)
class MemberPlanningStatus:
    """Compact per-member explanation of selection and truncation."""

    face_key: Tuple[Any, ...]
    candidate_pool_count: int
    emitted_count: int
    neighbor_candidate_count: int
    fallback_candidate_count: int
    valid_master_count: int
    reasons: Tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        return bool(set(self.reasons).intersection(_TRUNCATION_REASONS))


@dataclass(frozen=True)
class PlannerDiagnostics:
    """Compact run evidence; no graph, loop, or pair mapping is retained."""

    selected: int
    topology_buckets: int
    candidate_pairs: int
    theoretical_all_pairs: int
    avoided_all_pairs: int
    truncated_members: Tuple[Tuple[Any, ...], ...]
    truncated_buckets: Tuple[Any, ...]
    max_bucket: int
    estimated_bytes: int
    elapsed_ms: float
    unresolved_members: int
    reason_counts: Tuple[Tuple[str, int], ...]
    member_statuses: Tuple[MemberPlanningStatus, ...]

    @property
    def topology_bucket_count(self) -> int:
        return self.topology_buckets

    @property
    def max_bucket_size(self) -> int:
        return self.max_bucket

    @property
    def all_pairs(self) -> int:
        return self.theoretical_all_pairs

    @property
    def pairs_emitted(self) -> int:
        return self.candidate_pairs


@dataclass(frozen=True)
class FaceRefinementResult:
    """Immutable evidence for the bounded face-colour prefilter.

    ``fingerprint`` contains only canonical labels, partition profiles and
    digests.  It never contains the input face/edge/vertex positions or
    adapter-owned IDs.  Timing and termination fields are intentionally kept
    outside the fingerprint so repeated runs produce identical bucket keys.
    """

    fingerprint: Tuple[Any, ...]
    rounds: int
    max_rounds: int
    stable: bool
    truncated: bool
    elapsed_ms: float


@dataclass
class _CandidateLookup:
    candidates: Tuple[IslandRecord, ...]
    tiers: Dict[Tuple[Any, ...], str]
    neighbor_count: int
    fallback_count: int
    fallback_probe_limited: bool
    valid_master_count: int


class _BucketIndex:
    """Per-fingerprint index.  It stores records, not pair results."""

    def __init__(self, records: Sequence[IslandRecord], config: PlannerConfig) -> None:
        self.records = tuple(records)
        self.record_by_key = {record.face_key: record for record in self.records}
        self.valid_masters = tuple(
            sorted(
                (record for record in self.records if record.has_valid_density),
                key=cmp_to_key(lambda left, right: _compare_masters(left, right, config)),
            )
        )
        self.master_rank = {
            record.face_key: index for index, record in enumerate(self.valid_masters)
        }
        by_bin = defaultdict(list)
        for record in self.valid_masters:
            by_bin[_descriptor_bin(record, config)].append(record)
        self.by_bin = {
            key: tuple(
                sorted(
                    values,
                    key=lambda item: self.master_rank[item.face_key],
                )
            )
            for key, values in by_bin.items()
        }
        self.fallback_records = tuple(
            sorted(
                self.valid_masters,
                key=lambda item: (
                    _descriptor_key(item.normalized_boundary_descriptor),
                    self.master_rank[item.face_key],
                ),
            )
        )
        self.fallback_keys = tuple(
            _descriptor_key(record.normalized_boundary_descriptor)
            for record in self.fallback_records
        )

    def lookup(self, member: IslandRecord, config: PlannerConfig) -> _CandidateLookup:
        valid_count = len(self.valid_masters)
        if not valid_count:
            return _CandidateLookup((), {}, 0, 0, False, 0)

        wanted = config.per_member_k
        if wanted <= 0:
            return _CandidateLookup((), {}, 0, 0, False, valid_count)

        selected: Dict[Tuple[Any, ...], str] = {}
        bin_key = _descriptor_bin(member, config)
        if bin_key:
            neighbor_bins = _neighbor_bins(bin_key)
            heap = []
            head_limit = wanted + 1
            for neighbor_bin in neighbor_bins:
                records = self.by_bin.get(neighbor_bin, ())
                for record in records[:head_limit]:
                    heap.append(
                        (
                            self.master_rank[record.face_key],
                            _face_sort_key(record.face_key),
                            _face_sort_key(record.face_key),
                            record,
                        )
                    )
            heap.sort(key=lambda item: (item[0], item[1], item[2]))
            for _rank, _sort_key, _face_key_value, record in heap:
                if record.face_key == member.face_key:
                    continue
                if record.face_key in selected:
                    continue
                selected[record.face_key] = "neighbor_bin"
                if len(selected) >= wanted:
                    break

        neighbor_count = len(selected)
        fallback_count = 0
        fallback_probe_limited = False
        if len(selected) < wanted and self.fallback_records:
            descriptor = _descriptor_key(member.normalized_boundary_descriptor)
            position = bisect_left(self.fallback_keys, descriptor)
            probe_limit = min(config.fallback_probe_limit, len(self.fallback_records))
            probe_positions = []
            offset = 0
            max_attempts = max(1, len(self.fallback_records) * 2 + 1)
            while len(probe_positions) < probe_limit and offset < max_attempts:
                if offset == 0:
                    candidate_position = position
                else:
                    radius = (offset + 1) // 2
                    candidate_position = (
                        position - radius if offset % 2 == 1 else position + radius
                    )
                if (
                    0 <= candidate_position < len(self.fallback_records)
                    and candidate_position not in probe_positions
                ):
                    probe_positions.append(candidate_position)
                offset += 1
            if probe_limit < len(self.fallback_records):
                fallback_probe_limited = True

            fallback_candidates = []
            for candidate_position in probe_positions:
                record = self.fallback_records[candidate_position]
                if record.face_key == member.face_key or record.face_key in selected:
                    continue
                fallback_candidates.append(
                    (
                        _descriptor_distance(member, record),
                        self.master_rank[record.face_key],
                        _face_sort_key(record.face_key),
                        record,
                    )
                )
            fallback_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            fallback_limit = config.fallback_candidate_limit
            if fallback_limit <= 0:
                fallback_limit = wanted
            for _distance, _rank, _sort_key, record in fallback_candidates[:fallback_limit]:
                if len(selected) >= wanted:
                    break
                if record.face_key in selected:
                    continue
                selected[record.face_key] = "canonical_fallback"
                fallback_count += 1

        candidates = tuple(
            sorted(
                (self.record_by_key[key] for key in selected),
                key=lambda item: self.master_rank[item.face_key],
            )
        )
        return _CandidateLookup(
            candidates=candidates,
            tiers=selected,
            neighbor_count=neighbor_count,
            fallback_count=fallback_count,
            fallback_probe_limited=fallback_probe_limited,
            valid_master_count=valid_count,
        )


def _compare_masters(
    left: IslandRecord, right: IslandRecord, config: PlannerConfig
) -> int:
    left_density = left.density if left.density is not None else float("-inf")
    right_density = right.density if right.density is not None else float("-inf")
    difference = left_density - right_density
    if abs(difference) > config.density_tie_epsilon:
        return -1 if difference > 0.0 else 1
    left_key = _face_sort_key(left.face_key)
    right_key = _face_sort_key(right.face_key)
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def _descriptor_bin(record: IslandRecord, config: PlannerConfig) -> Tuple[int, ...]:
    values = record.normalized_boundary_descriptor[: config.index_dimensions]
    if not values:
        return ()
    return tuple(
        int(math.floor(float(value) / config.descriptor_bin_width))
        for value in values
    )


def _neighbor_bins(bin_key: Tuple[int, ...]) -> Tuple[Tuple[int, ...], ...]:
    if not bin_key:
        return ()
    return tuple(
        tuple(value + delta for value, delta in zip(bin_key, offset))
        for offset in itertools.product((-1, 0, 1), repeat=len(bin_key))
    )


def _descriptor_distance(left: IslandRecord, right: IslandRecord) -> float:
    left_values = left.normalized_boundary_descriptor
    right_values = right.normalized_boundary_descriptor
    if not left_values or not right_values:
        return 0.0 if left_values == right_values else float("inf")
    count = min(len(left_values), len(right_values))
    distance = sum(
        (float(left_values[index]) - float(right_values[index])) ** 2
        for index in range(count)
    )
    if len(left_values) != len(right_values):
        distance += float(abs(len(left_values) - len(right_values)))
    return distance


def _estimate_index_bytes(
    records: Sequence[IslandRecord], buckets: Mapping[Any, _BucketIndex]
) -> int:
    """Return a conservative, compact estimate for planner-owned structures."""

    record_bytes = sum(sys.getsizeof(record) for record in records)
    reference_count = sum(
        len(bucket.records)
        + len(bucket.valid_masters)
        + len(bucket.fallback_records)
        + sum(len(values) for values in bucket.by_bin.values())
        for bucket in buckets.values()
    )
    bin_count = sum(len(bucket.by_bin) for bucket in buckets.values())
    return int(
        record_bytes
        + sys.getsizeof(tuple(records))
        + sys.getsizeof(dict(buckets))
        + reference_count * sys.getsizeof(None)
        + bin_count * sys.getsizeof(())
    )


class CandidatePlan:
    """Reusable bounded stream over direct master candidates.

    The object keeps only the immutable records and the descriptor index.  A
    call to :meth:`iter_batches` performs one deterministic run and updates
    :attr:`diagnostics` as it completes.  It is safe to iterate again; pairs
    are recomputed rather than cached.
    """

    def __init__(self, records: Iterable[IslandRecord], config: PlannerConfig) -> None:
        started = time.perf_counter()
        normalized = tuple(
            item if isinstance(item, IslandRecord) else IslandRecord(
                item.face_key,
                item.strict_topology_fingerprint,
                item.normalized_boundary_descriptor,
                item.density,
                item.cheap_signature,
            )
            for item in records
        )
        ordered = tuple(sorted(normalized, key=lambda item: _face_sort_key(item.face_key)))
        seen = set()
        for record in ordered:
            if record.face_key in seen:
                raise ValueError("duplicate face_key: %r" % (record.face_key,))
            seen.add(record.face_key)
        self.config = config
        self.records = ordered
        grouped = defaultdict(list)
        for record in ordered:
            grouped[record.strict_topology_fingerprint].append(record)
        self._buckets = {
            fingerprint: _BucketIndex(values, config)
            for fingerprint, values in grouped.items()
        }
        self._bucket_for_key = {
            record.face_key: self._buckets[record.strict_topology_fingerprint]
            for record in ordered
        }
        self._fingerprint_for_key = {
            record.face_key: record.strict_topology_fingerprint
            for record in ordered
        }
        self._estimated_bytes = _estimate_index_bytes(ordered, self._buckets)
        self._build_elapsed_ms = (time.perf_counter() - started) * 1000.0
        sizes = [len(bucket.records) for bucket in self._buckets.values()]
        self._theoretical_all_pairs = sum(size * (size - 1) // 2 for size in sizes)
        self._last_diagnostics = PlannerDiagnostics(
            selected=len(ordered),
            topology_buckets=len(self._buckets),
            candidate_pairs=0,
            theoretical_all_pairs=self._theoretical_all_pairs,
            avoided_all_pairs=self._theoretical_all_pairs,
            truncated_members=(),
            truncated_buckets=(),
            max_bucket=max(sizes) if sizes else 0,
            estimated_bytes=self._estimated_bytes,
            elapsed_ms=self._build_elapsed_ms,
            unresolved_members=0,
            reason_counts=(),
            member_statuses=(),
        )

    @property
    def diagnostics(self) -> PlannerDiagnostics:
        return self._last_diagnostics

    @property
    def topology_buckets(self) -> int:
        return len(self._buckets)

    def iter_pairs(self) -> Iterator[CandidatePair]:
        for batch in self.iter_batches():
            for pair in batch:
                yield pair

    def __iter__(self) -> Iterator[CandidatePair]:
        return self.iter_pairs()

    def iter_batches(self, batch_size: Optional[int] = None) -> Iterator[Tuple[CandidatePair, ...]]:
        requested_batch_size = self.config.batch_size if batch_size is None else int(batch_size)
        if requested_batch_size <= 0:
            raise ValueError("batch_size must be positive")
        return self._iter_batches(requested_batch_size)

    def materialize(self) -> Tuple[Tuple[CandidatePair, ...], PlannerDiagnostics]:
        """Materialize pairs for tests/profile tooling that explicitly asks for it."""

        pairs = tuple(self.iter_pairs())
        return pairs, self.diagnostics

    def _iter_batches(self, batch_size: int) -> Iterator[Tuple[CandidatePair, ...]]:
        active_started = time.perf_counter()
        active_elapsed = 0.0
        suspended_at_yield = False
        batch: List[CandidatePair] = []
        pair_count = 0
        unresolved = 0
        reason_counts = Counter()
        statuses: List[MemberPlanningStatus] = []
        truncated_members: List[Tuple[Any, ...]] = []
        truncated_buckets = set()
        bucket_pair_counts = Counter()

        try:
            for member in self.records:
                bucket = self._bucket_for_key[member.face_key]
                bucket_fingerprint = self._fingerprint_for_key[member.face_key]
                reasons: List[str] = []
                lookup = _CandidateLookup((), {}, 0, 0, False, 0)

                if not member.has_valid_density:
                    reasons.append(REASON_INVALID_MEMBER_DENSITY)
                elif pair_count >= self.config.global_pair_budget:
                    reasons.append(REASON_GLOBAL_PAIR_BUDGET)
                elif bucket_pair_counts[bucket_fingerprint] >= self.config.per_bucket_pair_budget:
                    reasons.append(REASON_BUCKET_PAIR_BUDGET)
                    truncated_buckets.add(_compact_bucket_token(bucket_fingerprint))
                else:
                    lookup = bucket.lookup(member, self.config)
                    if not lookup.valid_master_count:
                        reasons.append(REASON_NO_VALID_MASTER)
                    elif lookup.valid_master_count <= 1 and member.has_valid_density:
                        reasons.append(REASON_NO_OTHER_MASTER)
                    if lookup.fallback_probe_limited:
                        reasons.append(REASON_FALLBACK_PROBE_LIMIT)
                    other_master_count = lookup.valid_master_count - int(member.has_valid_density)
                    if other_master_count > self.config.per_member_k:
                        reasons.append(REASON_PER_MEMBER_K)
                    if (
                        other_master_count > len(lookup.candidates)
                        and len(lookup.candidates) < self.config.per_member_k
                    ):
                        reasons.append(REASON_CANDIDATE_INDEX_LIMITED)

                remaining_global = max(
                    0,
                    self.config.global_pair_budget - pair_count,
                )
                remaining_bucket = max(
                    0,
                    self.config.per_bucket_pair_budget
                    - bucket_pair_counts[bucket_fingerprint],
                )
                emit_limit = min(
                    len(lookup.candidates),
                    self.config.per_member_k,
                    remaining_global,
                    remaining_bucket,
                )
                emitted = 0
                for candidate in lookup.candidates[:emit_limit]:
                    pair = CandidatePair(
                        member_key=member.face_key,
                        master_key=candidate.face_key,
                        rank=emitted + 1,
                        tier=lookup.tiers[candidate.face_key],
                        descriptor_distance=_descriptor_distance(member, candidate),
                        master_density=candidate.density,
                    )
                    batch.append(pair)
                    emitted += 1
                    pair_count += 1
                    bucket_pair_counts[bucket_fingerprint] += 1
                    if len(batch) >= batch_size:
                        active_elapsed += time.perf_counter() - active_started
                        suspended_at_yield = True
                        yield tuple(batch)
                        suspended_at_yield = False
                        active_started = time.perf_counter()
                        batch = []

                if emitted < min(len(lookup.candidates), self.config.per_member_k):
                    if remaining_global <= 0:
                        if REASON_GLOBAL_PAIR_BUDGET not in reasons:
                            reasons.append(REASON_GLOBAL_PAIR_BUDGET)
                    elif remaining_bucket <= 0:
                        if REASON_BUCKET_PAIR_BUDGET not in reasons:
                            reasons.append(REASON_BUCKET_PAIR_BUDGET)
                if not emitted and lookup.candidates and not reasons:
                    reasons.append(REASON_CANDIDATE_INDEX_LIMITED)
                if not emitted and not lookup.candidates:
                    unresolved += 1
                for reason in reasons:
                    reason_counts[reason] += 1
                status = MemberPlanningStatus(
                    face_key=member.face_key,
                    candidate_pool_count=len(lookup.candidates),
                    emitted_count=emitted,
                    neighbor_candidate_count=lookup.neighbor_count,
                    fallback_candidate_count=lookup.fallback_count,
                    valid_master_count=lookup.valid_master_count,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
                statuses.append(status)
                if status.truncated:
                    truncated_members.append(member.face_key)

            if batch:
                active_elapsed += time.perf_counter() - active_started
                suspended_at_yield = True
                yield tuple(batch)
                suspended_at_yield = False
                active_started = time.perf_counter()
        finally:
            if not suspended_at_yield:
                active_elapsed += time.perf_counter() - active_started
            elapsed_ms = active_elapsed * 1000.0 + self._build_elapsed_ms
            truncated_bucket_values = tuple(
                sorted(truncated_buckets, key=_stable_sort_key)
            )
            self._last_diagnostics = PlannerDiagnostics(
                selected=len(self.records),
                topology_buckets=len(self._buckets),
                candidate_pairs=pair_count,
                theoretical_all_pairs=self._theoretical_all_pairs,
                avoided_all_pairs=max(0, self._theoretical_all_pairs - pair_count),
                truncated_members=tuple(truncated_members),
                truncated_buckets=truncated_bucket_values,
                max_bucket=max(
                    (len(bucket.records) for bucket in self._buckets.values()),
                    default=0,
                ),
                estimated_bytes=self._estimated_bytes,
                elapsed_ms=elapsed_ms,
                unresolved_members=unresolved,
                reason_counts=tuple(sorted(reason_counts.items())),
                member_statuses=tuple(statuses),
            )


def plan_candidates(
    records: Iterable[IslandRecord], config: Optional[PlannerConfig] = None
) -> CandidatePlan:
    """Build a bounded candidate plan without materializing pair results."""

    return CandidatePlan(records, config or PlannerConfig())


def iter_candidate_batches(
    records: Iterable[IslandRecord],
    config: Optional[PlannerConfig] = None,
    *,
    batch_size: Optional[int] = None,
) -> Iterator[Tuple[CandidatePair, ...]]:
    """Convenience streaming API for callers that do not need the plan object."""

    plan = plan_candidates(records, config)
    return plan.iter_batches(batch_size=batch_size)


build_candidate_plan = plan_candidates


def generate_candidate_pairs(
    records: Iterable[IslandRecord], config: Optional[PlannerConfig] = None
) -> Iterator[CandidatePair]:
    """Convenience direct-pair stream with no result cache."""

    return plan_candidates(records, config).iter_pairs()


def canonical_cycle_signature(values: Iterable[Any]) -> Tuple[Any, ...]:
    """Return a rotation/reversal-invariant immutable cycle token.

    Face-loop order is an adapter detail.  The Pro record builder uses this
    helper for its local edge/vertex base labels so equivalent loop rotations
    and reversals cannot be split into different strict buckets.
    """

    frozen = tuple(
        _freeze(value, path="face_cycle[%d]" % index)
        for index, value in enumerate(tuple(values))
    )
    if len(frozen) < 2:
        return frozen
    candidates = []
    for sequence in (frozen, tuple(reversed(frozen))):
        for offset in range(len(sequence)):
            candidates.append(sequence[offset:] + sequence[:offset])
    return min(candidates, key=_stable_sort_key)


def _canonical_value_histogram(values: Iterable[Any]) -> Tuple[Tuple[Any, int], ...]:
    """Return a deterministic histogram for already-frozen primitive values."""

    frozen = tuple(_freeze(value, path="face_refinement.value") for value in values)
    return tuple(
        sorted(Counter(frozen).items(), key=lambda item: _stable_sort_key(item[0]))
    )


def _same_partition(left: Sequence[int], right: Sequence[int]) -> bool:
    """Compare partitions by equivalence, independent of colour numbering."""

    if len(left) != len(right):
        return False
    left_to_right = {}
    right_to_left = {}
    for left_colour, right_colour in zip(left, right):
        previous = left_to_right.setdefault(left_colour, right_colour)
        if previous != right_colour:
            return False
        previous = right_to_left.setdefault(right_colour, left_colour)
        if previous != left_colour:
            return False
    return True


def canonical_face_color_refinement(
    face_labels: Sequence[Any],
    adjacency: Sequence[Iterable[int]],
    *,
    edge_labels: Sequence[Any] = (),
    vertex_labels: Sequence[Any] = (),
    loop_count: int = 0,
    max_rounds: Optional[int] = None,
) -> FaceRefinementResult:
    """Build a bounded, ID-free Weisfeiler--Leman face invariant.

    ``face_labels`` contains immutable local/base labels and ``adjacency``
    contains face-to-face incidence by temporary positions.  Positions are
    used only while computing the invariant and are never emitted.  Each
    refinement label combines the prior partition with the sorted neighbour
    colour multiset.  A canonical transcript digest plus final partition
    profile makes the strict bucket stronger than a fixed two-round colour
    histogram while retaining exact correspondence as the final proof.

    The default bound is ``min(face_count, 64)``.  Since a strict partition
    can split at most ``face_count - 1`` times, the face-count-derived bound
    is finite; the cap is a resource guard for unusually large islands.  A
    capped result is still safe for prefiltering because it only fails to
    reject some non-isomorphic candidates.
    """

    started = time.perf_counter()
    labels = tuple(
        _freeze(value, path="face_labels[%d]" % index)
        for index, value in enumerate(tuple(face_labels))
    )
    if not labels:
        raise ValueError("face_labels must not be empty")
    raw_adjacency = tuple(adjacency)
    if len(raw_adjacency) != len(labels):
        raise ValueError("face_labels and adjacency length mismatch")
    normalized_adjacency = []
    for index, neighbours in enumerate(raw_adjacency):
        if isinstance(neighbours, (str, bytes)):
            raise TypeError("adjacency[%d] must be an iterable of integer positions" % index)
        values = []
        for neighbour in neighbours:
            if isinstance(neighbour, bool) or not isinstance(neighbour, numbers.Integral):
                raise TypeError("adjacency[%d] contains a non-integer position" % index)
            neighbour = int(neighbour)
            if neighbour < 0 or neighbour >= len(labels):
                raise ValueError("adjacency[%d] contains an out-of-range position" % index)
            values.append(neighbour)
        normalized_adjacency.append(tuple(sorted(set(values))))
    normalized_adjacency = tuple(normalized_adjacency)

    if isinstance(loop_count, bool) or not isinstance(loop_count, numbers.Integral):
        raise ValueError("loop_count must be a non-negative integer")
    loop_count = int(loop_count)
    if loop_count < 0:
        raise ValueError("loop_count must be a non-negative integer")
    for name, values in (("edge_labels", edge_labels), ("vertex_labels", vertex_labels)):
        if isinstance(values, (str, bytes)):
            raise TypeError("%s must be a sequence of primitive labels" % name)

    face_count = len(labels)
    if max_rounds is None:
        max_rounds = min(face_count, DEFAULT_FACE_REFINEMENT_MAX_ROUNDS)
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, numbers.Integral):
        raise ValueError("max_rounds must be a non-negative integer")
    max_rounds = int(max_rounds)
    if max_rounds < 0:
        raise ValueError("max_rounds must be a non-negative integer")

    def assign(values: Sequence[Any]) -> Tuple[int, ...]:
        unique = sorted(set(values), key=_stable_sort_key)
        colour_map = {value: index for index, value in enumerate(unique)}
        return tuple(colour_map[value] for value in values)

    def profile(colours: Sequence[int]) -> Tuple[Any, ...]:
        return tuple(
            sorted(
                (
                    (
                        labels[index],
                        int(colours[index]),
                        tuple(
                            sorted(
                                colours[neighbour] for neighbour in neighbours
                            )
                        ),
                    )
                    for index, neighbours in enumerate(normalized_adjacency)
                ),
                key=_stable_sort_key,
            )
        )

    def round_token(round_index: int, colours: Sequence[int]) -> Tuple[Any, ...]:
        canonical_profile = profile(colours)
        profile_digest = hashlib.sha256(
            repr(canonical_profile).encode("utf-8")
        ).hexdigest()
        return (
            int(round_index),
            _canonical_value_histogram(colours),
            profile_digest,
        )

    colours = assign(labels)
    transcript = [round_token(0, colours)]
    rounds = 0
    stable = False
    for round_index in range(1, max_rounds + 1):
        next_labels = tuple(
            (
                labels[index],
                tuple(
                    sorted(colours[neighbour] for neighbour in neighbours)
                ),
            )
            for index, neighbours in enumerate(normalized_adjacency)
        )
        next_colours = assign(next_labels)
        transcript.append(round_token(round_index, next_colours))
        rounds = round_index
        if _same_partition(colours, next_colours):
            colours = next_colours
            stable = True
            break
        colours = next_colours
    truncated = not stable

    edge_values = tuple(
        _freeze(value, path="edge_labels[%d]" % index)
        for index, value in enumerate(tuple(edge_labels))
    )
    vertex_values = tuple(
        _freeze(value, path="vertex_labels[%d]" % index)
        for index, value in enumerate(tuple(vertex_labels))
    )
    final_partition = _canonical_value_histogram(
        (labels[index], colours[index]) for index in range(face_count)
    )
    degree_histogram = _canonical_value_histogram(
        len(neighbours) for neighbours in normalized_adjacency
    )
    transcript_value = (
        "pro-face-wl-v5",
        (face_count, len(edge_values), len(vertex_values), loop_count),
        _canonical_value_histogram(edge_values),
        _canonical_value_histogram(vertex_values),
        _canonical_value_histogram(labels),
        degree_histogram,
        tuple(transcript),
        final_partition,
        bool(stable),
        bool(truncated),
        max_rounds,
    )
    digest = hashlib.sha256(repr(transcript_value).encode("utf-8")).hexdigest()
    fingerprint = (
        "pro-face-wl-v5",
        transcript_value[1],
        transcript_value[2],
        transcript_value[3],
        transcript_value[4],
        transcript_value[5],
        transcript_value[6],
        transcript_value[7],
        bool(stable),
        bool(truncated),
        max_rounds,
        digest,
    )
    return FaceRefinementResult(
        fingerprint=fingerprint,
        rounds=rounds,
        max_rounds=max_rounds,
        stable=stable,
        truncated=truncated,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


def _graph_node_id(kind: str, key: Any) -> Tuple[str, Any]:
    return kind, _freeze(key, path="graph.%s.key" % kind)


def _add_relation(
    adjacency: Dict[Tuple[str, Any], List[Tuple[str, Tuple[str, Any]]]],
    left: Tuple[str, Any],
    relation: str,
    right: Tuple[str, Any],
    reverse_relation: str,
) -> None:
    adjacency[left].append((relation, right))
    adjacency[right].append((reverse_relation, left))


def _graph_signature_value(value: Any) -> Any:
    return _freeze(value, path="graph.signature")


def canonical_graph_color_signature(
    graph: Any, rounds: int = DEFAULT_GRAPH_REFINEMENT_ROUNDS
) -> Tuple[Any, ...]:
    """Return a compact graph-aware strict topology fingerprint.

    The input is expected to be an immutable graph with the record shape used
    by ``uv_gpt.topology_correspondence``.  Node keys never enter the output.
    Initial typed incidence labels are refined through directed face/edge/
    vertex/boundary/cycle relations.  A SHA-256 digest of the canonical
    refinement transcript keeps the record compact while the node-count and
    color histograms make the fingerprint easy to inspect in diagnostics.
    This is a strict pre-bucket, not a replacement for exact correspondence.
    """

    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
        raise ValueError("rounds must be a non-negative integer")

    nodes: Dict[Tuple[str, Any], Tuple[Any, ...]] = {}
    adjacency: Dict[Tuple[str, Any], List[Tuple[str, Tuple[str, Any]]]] = defaultdict(list)

    def add_node(kind: str, key: Any, label: Tuple[Any, ...]) -> Tuple[str, Any]:
        node = _graph_node_id(kind, key)
        if node in nodes:
            raise ValueError("duplicate graph node: %r" % (node,))
        nodes[node] = (kind,) + tuple(_graph_signature_value(item) for item in label)
        adjacency[node]
        return node

    faces = tuple(getattr(graph, "faces", ()))
    edges = tuple(getattr(graph, "edges", ()))
    vertices = tuple(getattr(graph, "vertices", ()))
    loops = tuple(getattr(graph, "loops", ()))
    boundaries = tuple(getattr(graph, "boundaries", ()))
    if not faces or not edges or not vertices or not loops:
        raise ValueError("graph must contain faces, edges, vertices, and loops")

    face_nodes = {
        item.key: add_node("face", item.key, (len(item.loop_keys), item.signature))
        for item in faces
    }
    edge_nodes = {
        item.key: add_node(
            "edge",
            item.key,
            (
                len(item.loop_keys),
                len(item.face_keys),
                bool(item.boundary),
                bool(item.non_manifold),
                item.signature,
            ),
        )
        for item in edges
    }
    vertex_nodes = {
        item.key: add_node(
            "vertex",
            item.key,
            (len(item.loop_keys), bool(item.boundary), item.signature),
        )
        for item in vertices
    }
    loop_nodes = {
        item.key: add_node(
            "loop",
            item.key,
            (bool(item.boundary), bool(item.seam), item.signature),
        )
        for item in loops
    }
    boundary_nodes = {
        item.key: add_node(
            "boundary",
            item.key,
            (item.role, len(item.loop_keys), item.signature, item.parent_key is not None),
        )
        for item in boundaries
    }

    for item in faces:
        face_node = face_nodes[item.key]
        for loop_key in item.loop_keys:
            _add_relation(adjacency, face_node, "face_loop", loop_nodes[loop_key], "loop_face")
    for item in edges:
        edge_node = edge_nodes[item.key]
        for loop_key in item.loop_keys:
            _add_relation(adjacency, edge_node, "edge_loop", loop_nodes[loop_key], "loop_edge")
    for item in vertices:
        vertex_node = vertex_nodes[item.key]
        for loop_key in item.loop_keys:
            _add_relation(
                adjacency,
                vertex_node,
                "vertex_loop",
                loop_nodes[loop_key],
                "loop_vertex",
            )
    for item in loops:
        loop_node = loop_nodes[item.key]
        _add_relation(adjacency, loop_node, "loop_next", loop_nodes[item.next_key], "loop_prev")
        _add_relation(adjacency, loop_node, "loop_prev", loop_nodes[item.prev_key], "loop_next")
    for item in boundaries:
        boundary_node = boundary_nodes[item.key]
        for loop_key in item.loop_keys:
            _add_relation(
                adjacency,
                boundary_node,
                "boundary_loop",
                loop_nodes[loop_key],
                "loop_boundary",
            )
        if item.parent_key is not None:
            _add_relation(
                adjacency,
                boundary_node,
                "boundary_parent",
                boundary_nodes[item.parent_key],
                "boundary_child",
            )

    def assign_colors(labels: Mapping[Tuple[str, Any], Any]) -> Dict[Tuple[str, Any], str]:
        unique = sorted(set(labels.values()), key=_stable_sort_key)
        color_map = {value: "c%06d" % index for index, value in enumerate(unique)}
        return {node: color_map[label] for node, label in labels.items()}

    labels = dict(nodes)
    colors = assign_colors(labels)
    transcript = []
    for _round in range(rounds + 1):
        histogram = tuple(
            sorted(
                Counter((nodes[node][0], colors[node]) for node in nodes).items(),
                key=_stable_sort_key,
            )
        )
        transcript.append(histogram)
        next_labels = {}
        for node in sorted(nodes, key=_stable_sort_key):
            neighborhood = tuple(
                sorted(
                    (relation, colors[neighbor])
                    for relation, neighbor in adjacency[node]
                )
            )
            next_labels[node] = (nodes[node], neighborhood)
        new_colors = assign_colors(next_labels)
        if new_colors == colors:
            colors = new_colors
            break
        colors = new_colors

    final_profile = tuple(
        sorted(
            (
                (
                    nodes[node][0],
                    colors[node],
                    tuple(
                        sorted(
                            (relation, colors[neighbor])
                            for relation, neighbor in adjacency[node]
                        )
                    ),
                )
                for node in nodes
            ),
            key=_stable_sort_key,
        )
    )
    counts = tuple(
        sorted(
            (
                kind,
                sum(1 for node in nodes if node[0] == kind),
            )
            for kind in sorted({node[0] for node in nodes})
        )
    )
    transcript_value = ("pro-wl-v1", counts, tuple(transcript), final_profile)
    digest = hashlib.sha256(repr(transcript_value).encode("utf-8")).hexdigest()
    return ("pro-wl-v1", counts, tuple(transcript), digest)


strict_topology_fingerprint = canonical_graph_color_signature


__all__ = [
    "CandidatePair",
    "CandidatePlan",
    "CandidatePlannerConfig",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_DESCRIPTOR_BIN_WIDTH",
    "DEFAULT_FACE_REFINEMENT_MAX_ROUNDS",
    "DEFAULT_GLOBAL_PAIR_BUDGET",
    "DEFAULT_PER_BUCKET_PAIR_BUDGET",
    "DEFAULT_PER_MEMBER_K",
    "MAX_FALLBACK_PROBE_LIMIT",
    "REASON_BUCKET_PAIR_BUDGET",
    "REASON_CANDIDATE_INDEX_LIMITED",
    "REASON_FALLBACK_PROBE_LIMIT",
    "REASON_GLOBAL_PAIR_BUDGET",
    "REASON_INVALID_MEMBER_DENSITY",
    "REASON_NO_OTHER_MASTER",
    "REASON_NO_VALID_MASTER",
    "REASON_PER_MEMBER_K",
    "IslandRecord",
    "FaceRefinementResult",
    "MemberPlanningStatus",
    "PlannerConfig",
    "PlannerDiagnostics",
    "canonical_cycle_signature",
    "canonical_face_color_refinement",
    "canonical_graph_color_signature",
    "build_candidate_plan",
    "generate_candidate_pairs",
    "iter_candidate_batches",
    "plan_candidates",
    "strict_topology_fingerprint",
]
