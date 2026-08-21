"""Pure group-first planning for Align Similar Pro.

The live Blender session is deliberately not imported here.  This module
contains the deterministic part of the Pro contract:

* islands are ordered by structural bucket and stable face key;
* each item is compared only with the fixed representatives that precede it;
* comparison results may arrive in any order, but grouping advances in the
  canonical item order;
* groups are re-rooted at their largest valid UV-area island; and
* one direct exact job is emitted for every non-master member.

The shape result carried by :class:`GroupPairResult` is intentionally a
small frozen record.  A caller may keep richer matcher evidence separately,
but no mutable Blender object can cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import hashlib
import math
import pickle
from typing import Any, Iterable, Mapping, Optional, Sequence

try:
    from . import pro_similarity_seed as _seed_algebra
except ImportError:  # pragma: no cover - direct pure-module imports
    import pro_similarity_seed as _seed_algebra


SCHEMA_VERSION = "uv-gpt-pro-group-first-v1"
ALGORITHM_VERSION = "normal-fixed-representative-uv-area-v1"
DEFAULT_DENSITY_TIE_EPSILON = 1.0e-12
DEFAULT_UV_AREA_TIE_EPSILON = 1.0e-12
_SEED_PROBE_POINTS = (
    (-1.25, 0.5),
    (0.0, 0.0),
    (0.375, -0.875),
    (2.0, 1.5),
)
_SEED_VERIFY_ABS_TOLERANCE = 1.0e-9
_SEED_VERIFY_REL_TOLERANCE = 1.0e-9


class GroupFirstError(ValueError):
    """Base error for an invalid or incomplete group-first plan."""


class UnknownPairResultError(GroupFirstError):
    """A result was received for a pair that this frontier did not request."""


class ConflictingPairResultError(GroupFirstError):
    """The same canonical pair was delivered with different results."""


class IncompleteGroupPlanError(GroupFirstError):
    """The caller asked for a final plan before all required results arrived."""


def _stable_token(value: Any) -> tuple[str, str]:
    """Return a total ordering token without relying on mixed-type ordering."""

    return (type(value).__name__, repr(value))


def _normalize(value: Any, *, path: str = "value") -> Any:
    """Freeze primitive containers while keeping ordinary scalar values readable."""

    if value is None or isinstance(value, (bool, str, bytes, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GroupFirstError("%s contains a non-finite float" % path)
        return float(value)
    if isinstance(value, (tuple, list)):
        return tuple(
            _normalize(item, path="%s[%d]" % (path, index))
            for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        entries = [
            (
                _normalize(key, path="%s.key" % path),
                _normalize(item, path="%s.value" % path),
            )
            for key, item in value.items()
        ]
        return tuple(sorted(entries, key=lambda item: _stable_token(item[0])))
    if isinstance(value, (set, frozenset)):
        return tuple(
            sorted(
                (_normalize(item, path="%s.item" % path) for item in value),
                key=_stable_token,
            )
        )
    if is_dataclass(value):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not getattr(params, "frozen", False):
            raise GroupFirstError("%s contains a mutable dataclass" % path)
        return tuple(
            (
                field.name,
                _normalize(getattr(value, field.name), path="%s.%s" % (path, field.name)),
            )
            for field in fields(value)
        )
    raise GroupFirstError(
        "%s must contain only primitive immutable values, got %s"
        % (path, type(value).__name__)
    )


def _key(value: Any, *, path: str = "key") -> tuple[Any, ...]:
    """Normalize a face/island key to a hashable tuple."""

    frozen = _normalize(value, path=path)
    if isinstance(frozen, tuple):
        return frozen
    return (frozen,)


def _key_token(value: Any) -> tuple[str, str]:
    return _stable_token(_key(value))


def _canonical(value: Any) -> Any:
    """Canonicalize a frozen value for an insertion-order-independent digest."""

    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int) and not isinstance(value, bool):
        return ("int", str(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return ("float", repr(value).lower())
        return ("float", value.hex())
    if isinstance(value, (str, bytes)):
        return (type(value).__name__, value)
    if isinstance(value, Mapping):
        items = [(_canonical(key), _canonical(item)) for key, item in value.items()]
        return ("map", tuple(sorted(items, key=repr)))
    if isinstance(value, (tuple, list)):
        return ("seq", tuple(_canonical(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted((_canonical(item) for item in value), key=repr)))
    raise GroupFirstError("unsupported digest value: %s" % type(value).__name__)


def stable_digest(value: Any) -> str:
    """Return a stable uppercase SHA-256 digest for frozen semantic data."""

    encoded = pickle.dumps(_canonical(value), protocol=5)
    return hashlib.sha256(encoded).hexdigest().upper()


def _valid_density(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        density = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(density) or density <= 0.0:
        return None
    return density


def _valid_uv_area(value: Any) -> Optional[float]:
    """Return a finite non-negative UV polygon area, or ``None``.

    UV area is deliberately independent from mesh/world area and texel
    density.  A finite zero is retained as a valid (degenerate) measurement;
    an absent or invalid measurement simply makes the area master unavailable
    so the caller can skip the direct job safely.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        area = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(area) or area < 0.0:
        return None
    return area


def _get(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _transform_seed(value: Any) -> Optional[tuple[Any, ...]]:
    """Freeze the geometry-only candidate-to-reference transform evidence.

    Shape workers expose either ``reference_center``/``candidate_center`` or
    the topology solver's ``target_center``/``source_center`` names.  Keeping
    only these five primitive values makes the seed portable and avoids
    accidentally treating matcher score/timing fields as write evidence.
    """

    if value is None:
        return None

    def lookup(source: Any, *names: str) -> Any:
        if isinstance(source, Mapping):
            for name in names:
                if name in source:
                    return source[name]
            return None
        for name in names:
            if hasattr(source, name):
                return getattr(source, name)
        return None

    # ``GroupPairResult`` may already have frozen a dataclass into named
    # entries.  Turn those entries back into a small mapping for the common
    # transform representations.
    source = value
    if isinstance(value, (tuple, list)):
        if len(value) == 5 and not all(
            isinstance(item, (tuple, list)) and len(item) == 2 for item in value
        ):
            source = value
        elif all(isinstance(item, (tuple, list)) and len(item) == 2 for item in value):
            source = {item[0]: item[1] for item in value}

    if isinstance(source, (tuple, list)) and len(source) == 5:
        angle, scale, reflected, source_center, target_center = source
    else:
        angle = lookup(source, "angle")
        scale = lookup(source, "scale")
        reflected = lookup(source, "reflected")
        source_center = lookup(source, "candidate_center", "source_center")
        target_center = lookup(source, "reference_center", "target_center")
    if angle is None or scale is None or reflected is None:
        return None
    if source_center is None or target_center is None:
        return None
    try:
        angle = float(angle)
        scale = float(scale)
        source_center = (float(source_center[0]), float(source_center[1]))
        target_center = (float(target_center[0]), float(target_center[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not math.isfinite(angle) or not math.isfinite(scale):
        return None
    if not all(math.isfinite(item) for item in source_center + target_center):
        return None
    return (angle, scale, bool(reflected), source_center, target_center)


def _seed_close(left: Any, right: Any) -> bool:
    """Compare two finite seed applications without weakening the seed gate."""

    if left is None or right is None:
        return False
    for left_point, right_point in zip(left, right):
        if not math.isfinite(float(left_point)) or not math.isfinite(float(right_point)):
            return False
        limit = _SEED_VERIFY_ABS_TOLERANCE + _SEED_VERIFY_REL_TOLERANCE * max(
            1.0, abs(float(left_point)), abs(float(right_point))
        )
        if abs(float(left_point) - float(right_point)) > limit:
            return False
    return True


def _validate_reroot_seed(
    master_to_representative: Any,
    member_to_representative: Any,
    member_to_master: Any,
) -> bool:
    """Prove the composed seed agrees with both component transforms."""

    if member_to_master is None:
        return False
    master_leg = (
        _seed_algebra.identity_seed()
        if master_to_representative is None
        else _seed_algebra.inverse_seed(master_to_representative)
    )
    member_leg = (
        _seed_algebra.identity_seed()
        if member_to_representative is None
        else _seed_algebra.canonical_seed(member_to_representative)
    )
    if master_leg is None or member_leg is None:
        return False
    for point in _SEED_PROBE_POINTS:
        direct = _seed_algebra.apply_seed(member_to_master, point)
        via_representative = _seed_algebra.apply_seed(
            master_leg,
            _seed_algebra.apply_seed(member_leg, point),
        )
        if not _seed_close(direct, via_representative):
            return False
    return True


@dataclass(frozen=True)
class IslandRecord:
    """Immutable island input used by the group frontier.

    ``bucket_key`` is the strict structural bucket produced by the caller's
    existing cheap grouping logic.  The default empty tuple places all
    records in one bucket, which is useful for pure tests.
    """

    key: tuple[Any, ...]
    density: Optional[float] = None
    bucket_key: Any = ()
    ordinal: int = 0
    descriptor_digest: str = ""
    metadata: Any = ()
    uv_area: Optional[float] = None
    uv_size: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _key(self.key, path="island.key"))
        object.__setattr__(
            self, "bucket_key", _normalize(self.bucket_key, path="island.bucket_key")
        )
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise GroupFirstError("island.ordinal must be an integer")
        object.__setattr__(self, "density", _valid_density(self.density))
        if not isinstance(self.descriptor_digest, str):
            raise GroupFirstError("island.descriptor_digest must be text")
        object.__setattr__(
            self, "metadata", _normalize(self.metadata, path="island.metadata")
        )
        raw_area = self.uv_area if self.uv_area is not None else self.uv_size
        area = _valid_uv_area(raw_area)
        object.__setattr__(self, "uv_area", area)
        object.__setattr__(self, "uv_size", area)

    @property
    def face_key(self) -> tuple[Any, ...]:
        """Compatibility alias used by the existing Pro island records."""

        return self.key

    @property
    def structural_bucket(self) -> Any:
        return self.bucket_key


@dataclass(frozen=True, init=False)
class GroupPairRequest:
    """One canonical representative-to-future shape comparison request."""

    pair_ordinal: int
    representative_key: tuple[Any, ...]
    candidate_key: tuple[Any, ...]
    bucket_key: Any

    def __init__(
        self,
        pair_ordinal: Optional[int] = None,
        representative_key: Any = None,
        candidate_key: Any = None,
        bucket_key: Any = (),
        *,
        ordinal: Optional[int] = None,
        master_key: Any = None,
        member_key: Any = None,
    ) -> None:
        if pair_ordinal is None:
            pair_ordinal = ordinal
        if representative_key is None:
            representative_key = master_key
        if candidate_key is None:
            candidate_key = member_key
        if (
            isinstance(pair_ordinal, bool)
            or not isinstance(pair_ordinal, int)
            or pair_ordinal < 0
        ):
            raise GroupFirstError("pair_ordinal must be a non-negative integer")
        if representative_key is None or candidate_key is None:
            raise GroupFirstError("pair request keys are required")
        object.__setattr__(self, "pair_ordinal", pair_ordinal)
        object.__setattr__(
            self,
            "representative_key",
            _key(representative_key, path="pair.representative_key"),
        )
        object.__setattr__(
            self, "candidate_key", _key(candidate_key, path="pair.candidate_key")
        )
        object.__setattr__(self, "bucket_key", _normalize(bucket_key, path="pair.bucket_key"))

    @property
    def ordinal(self) -> int:
        return self.pair_ordinal

    @property
    def master_key(self) -> tuple[Any, ...]:
        return self.representative_key

    @property
    def member_key(self) -> tuple[Any, ...]:
        return self.candidate_key

    @property
    def pair_key(self) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        return (self.representative_key, self.candidate_key)


PairRequest = GroupPairRequest
ShapePairRequest = GroupPairRequest


@dataclass(frozen=True, init=False)
class GroupPairResult:
    """Frozen shape result consumed by :class:`GroupFirstFrontier`."""

    pair_ordinal: int
    representative_key: tuple[Any, ...]
    candidate_key: tuple[Any, ...]
    accepted: bool
    score: float
    reason: str
    transform: Any
    evidence: Any

    def __init__(
        self,
        pair_ordinal: Optional[int] = None,
        representative_key: Any = None,
        candidate_key: Any = None,
        accepted: bool = False,
        score: Any = math.inf,
        reason: str = "",
        transform: Any = None,
        evidence: Any = (),
        *,
        ordinal: Optional[int] = None,
        master_key: Any = None,
        member_key: Any = None,
        shape_result: Any = None,
        result: Any = None,
    ) -> None:
        if pair_ordinal is None:
            pair_ordinal = ordinal
        if representative_key is None:
            representative_key = master_key
        if candidate_key is None:
            candidate_key = member_key
        source = shape_result if shape_result is not None else result
        if source is not None:
            if representative_key is None:
                representative_key = _get(source, "representative_key", "master_key")
            if candidate_key is None:
                candidate_key = _get(source, "candidate_key", "member_key")
            accepted = bool(_get(source, "accepted", default=accepted))
            score = _get(source, "score", default=score)
            reason = _get(source, "reason", default=reason) or ""
            transform = _get(source, "transform", default=transform)
            if evidence == ():
                evidence = _get(source, "evidence", "diagnostics", default=())
        if (
            isinstance(pair_ordinal, bool)
            or not isinstance(pair_ordinal, int)
            or pair_ordinal < 0
        ):
            raise GroupFirstError("pair_ordinal must be a non-negative integer")
        if representative_key is None or candidate_key is None:
            raise GroupFirstError("pair result keys are required")
        if not isinstance(accepted, bool):
            raise GroupFirstError("pair result accepted must be bool")
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            numeric_score = math.inf
        if math.isnan(numeric_score):
            numeric_score = math.inf
        if not isinstance(reason, str):
            reason = str(reason)
        object.__setattr__(self, "pair_ordinal", pair_ordinal)
        object.__setattr__(
            self,
            "representative_key",
            _key(representative_key, path="result.representative_key"),
        )
        object.__setattr__(
            self, "candidate_key", _key(candidate_key, path="result.candidate_key")
        )
        object.__setattr__(self, "accepted", accepted)
        object.__setattr__(self, "score", numeric_score)
        object.__setattr__(self, "reason", reason)
        transform_seed = _transform_seed(transform)
        object.__setattr__(self, "transform", transform_seed)
        object.__setattr__(self, "evidence", _normalize(evidence, path="result.evidence"))

    @classmethod
    def from_match_result(cls, request: GroupPairRequest, value: Any) -> "GroupPairResult":
        return cls(
            pair_ordinal=request.pair_ordinal,
            representative_key=request.representative_key,
            candidate_key=request.candidate_key,
            shape_result=value,
        )

    @property
    def ordinal(self) -> int:
        return self.pair_ordinal

    @property
    def master_key(self) -> tuple[Any, ...]:
        return self.representative_key

    @property
    def member_key(self) -> tuple[Any, ...]:
        return self.candidate_key

    @property
    def pair_key(self) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        return (self.representative_key, self.candidate_key)


ShapePairResult = GroupPairResult
GroupMatchResult = GroupPairResult


@dataclass(frozen=True)
class DirectExactJob:
    """One and only one exact correspondence request for a group member."""

    job_ordinal: int
    master_key: tuple[Any, ...]
    member_key: tuple[Any, ...]
    group_index: int
    source_representative_key: tuple[Any, ...]
    shape_pair_ordinal: Optional[int] = None
    master_density: Optional[float] = None
    member_density: Optional[float] = None
    master_uv_area: Optional[float] = None
    member_uv_area: Optional[float] = None
    seed_transform: Any = None
    seed_source: str = "missing"
    seed_missing_reason: Optional[str] = None
    seed_identity_leg_used: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.job_ordinal, bool)
            or not isinstance(self.job_ordinal, int)
            or self.job_ordinal < 0
        ):
            raise GroupFirstError("job_ordinal must be non-negative")
        object.__setattr__(self, "master_key", _key(self.master_key, path="job.master_key"))
        object.__setattr__(self, "member_key", _key(self.member_key, path="job.member_key"))
        object.__setattr__(self, "source_representative_key", _key(self.source_representative_key, path="job.source_representative_key"))
        if (
            isinstance(self.group_index, bool)
            or not isinstance(self.group_index, int)
            or self.group_index < 0
        ):
            raise GroupFirstError("group_index must be non-negative")
        if self.shape_pair_ordinal is not None and (
            isinstance(self.shape_pair_ordinal, bool) or self.shape_pair_ordinal < 0
        ):
            raise GroupFirstError("shape_pair_ordinal must be non-negative")
        object.__setattr__(self, "master_density", _valid_density(self.master_density))
        object.__setattr__(self, "member_density", _valid_density(self.member_density))
        object.__setattr__(self, "master_uv_area", _valid_uv_area(self.master_uv_area))
        object.__setattr__(self, "member_uv_area", _valid_uv_area(self.member_uv_area))
        object.__setattr__(self, "seed_transform", _transform_seed(self.seed_transform))
        source = self.seed_source if isinstance(self.seed_source, str) else str(self.seed_source)
        if not source:
            source = "missing"
        object.__setattr__(self, "seed_source", source)
        reason = self.seed_missing_reason
        if reason is not None and not isinstance(reason, str):
            reason = str(reason)
        if self.seed_transform is not None:
            reason = None
        elif reason is None:
            reason = "seed_unavailable"
        object.__setattr__(self, "seed_missing_reason", reason)
        object.__setattr__(self, "seed_identity_leg_used", bool(self.seed_identity_leg_used))

    @property
    def ordinal(self) -> int:
        return self.job_ordinal

    @property
    def pair_ordinal(self) -> int:
        return self.job_ordinal

    @property
    def master_uv_size(self) -> Optional[float]:
        return self.master_uv_area

    @property
    def member_uv_size(self) -> Optional[float]:
        return self.member_uv_area


@dataclass(frozen=True)
class GroupRecord:
    """A fixed-representative group and its deterministic UV-area root.

    The density fields are retained as diagnostics for backwards-compatible
    reports.  They never select ``master_key`` or a direct exact job.
    """

    group_index: int
    bucket_key: Any
    representative_key: tuple[Any, ...]
    member_keys: tuple[tuple[Any, ...], ...]
    density_master_key: Optional[tuple[Any, ...]]
    density_master_density: Optional[float]
    member_shape_pair_ordinals: tuple[Optional[int], ...] = ()
    uv_area_master_key: Optional[tuple[Any, ...]] = None
    uv_area_master_area: Optional[float] = None

    def __post_init__(self) -> None:
        if isinstance(self.group_index, bool) or self.group_index < 0:
            raise GroupFirstError("group_index must be non-negative")
        object.__setattr__(self, "bucket_key", _normalize(self.bucket_key, path="group.bucket_key"))
        object.__setattr__(self, "representative_key", _key(self.representative_key, path="group.representative_key"))
        members = tuple(_key(value, path="group.member_key") for value in self.member_keys)
        if len(set(members)) != len(members):
            raise GroupFirstError("group contains duplicate member keys")
        if self.representative_key in members:
            raise GroupFirstError("representative must not appear in member_keys")
        object.__setattr__(self, "member_keys", members)
        if self.density_master_key is not None:
            object.__setattr__(self, "density_master_key", _key(self.density_master_key, path="group.density_master_key"))
        object.__setattr__(self, "density_master_density", _valid_density(self.density_master_density))
        object.__setattr__(self, "member_shape_pair_ordinals", tuple(self.member_shape_pair_ordinals))
        if self.uv_area_master_key is not None:
            object.__setattr__(self, "uv_area_master_key", _key(self.uv_area_master_key, path="group.uv_area_master_key"))
        object.__setattr__(self, "uv_area_master_area", _valid_uv_area(self.uv_area_master_area))

    @property
    def all_keys(self) -> tuple[tuple[Any, ...], ...]:
        return (self.representative_key,) + self.member_keys

    @property
    def keys(self) -> tuple[tuple[Any, ...], ...]:
        return self.all_keys

    @property
    def members(self) -> tuple[tuple[Any, ...], ...]:
        return self.member_keys

    @property
    def size(self) -> int:
        return len(self.member_keys) + 1

    @property
    def master_key(self) -> Optional[tuple[Any, ...]]:
        return self.uv_area_master_key

    @property
    def uv_size_master_key(self) -> Optional[tuple[Any, ...]]:
        return self.uv_area_master_key

    @property
    def uv_size_master_area(self) -> Optional[float]:
        return self.uv_area_master_area

    @property
    def uv_area(self) -> Optional[float]:
        """Compatibility spelling for the selected group's master size."""

        return self.uv_area_master_area

    @property
    def uv_size(self) -> Optional[float]:
        """Compatibility spelling for the selected group's master size."""

        return self.uv_area_master_area


@dataclass(frozen=True)
class GroupFirstPlan:
    """Immutable finalized group and direct-exact plan."""

    islands: tuple[IslandRecord, ...]
    groups: tuple[GroupRecord, ...]
    exact_jobs: tuple[DirectExactJob, ...]
    comparisons_planned: int
    comparisons_completed: int
    accepted_comparisons: int
    rejected_comparisons: int
    exact_job_bound: int
    membership_digest: str
    exact_jobs_digest: str
    complete: bool = True
    uv_area_by_key: tuple[tuple[tuple[Any, ...], Optional[float]], ...] = ()
    uv_size_by_key: tuple[tuple[tuple[Any, ...], Optional[float]], ...] = ()

    @property
    def direct_exact_jobs(self) -> tuple[DirectExactJob, ...]:
        return self.exact_jobs

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def selected_count(self) -> int:
        return len(self.islands)

    @property
    def exact_job_count(self) -> int:
        return len(self.exact_jobs)

    @property
    def digest(self) -> str:
        return self.membership_digest

    @property
    def group_membership_digest(self) -> str:
        return self.membership_digest

    @property
    def uv_area(self) -> tuple[tuple[tuple[Any, ...], Optional[float]], ...]:
        return self.uv_area_by_key

    @property
    def uv_size(self) -> tuple[tuple[tuple[Any, ...], Optional[float]], ...]:
        return self.uv_size_by_key

    @property
    def seed_planned(self) -> int:
        return sum(1 for job in self.exact_jobs if job.seed_transform is not None)

    @property
    def seed_rerooted(self) -> int:
        return sum(
            1
            for job in self.exact_jobs
            if job.seed_transform is not None and job.seed_source == "rerooted"
        )

    @property
    def seed_identity_leg(self) -> int:
        return sum(
            1
            for job in self.exact_jobs
            if job.seed_transform is not None and job.seed_identity_leg_used
        )

    @property
    def seed_missing_by_reason(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for job in self.exact_jobs:
            if job.seed_transform is not None:
                continue
            reason = str(job.seed_missing_reason or "seed_unavailable")
            counts[reason] = counts.get(reason, 0) + 1
        return tuple(sorted(counts.items(), key=lambda item: item[0]))

    @property
    def seed_digest(self) -> str:
        return stable_digest(
            (
                "uv-gpt-seed-evidence-v1",
                tuple(
                    (
                        job.job_ordinal,
                        job.master_key,
                        job.member_key,
                        job.seed_source,
                        job.seed_missing_reason,
                        job.seed_identity_leg_used,
                        job.seed_transform,
                    )
                    for job in self.exact_jobs
                ),
            )
        )

    def validate(self) -> None:
        keys = tuple(item.key for item in self.islands)
        if len(set(keys)) != len(keys):
            raise GroupFirstError("island keys are not unique")
        known = set(keys)
        seen: set[tuple[Any, ...]] = set()
        for group in self.groups:
            for item_key in group.all_keys:
                if item_key not in known or item_key in seen:
                    raise GroupFirstError("group partition is not a disjoint island partition")
                seen.add(item_key)
        if seen != known:
            raise GroupFirstError("group partition does not cover all islands")
        job_members: set[tuple[Any, ...]] = set()
        group_by_key = {
            item_key: group
            for group in self.groups
            for item_key in group.all_keys
        }
        if self.exact_job_count > self.exact_job_bound:
            raise GroupFirstError("direct exact job bound exceeded")
        for job in self.exact_jobs:
            if job.master_key == job.member_key:
                raise GroupFirstError("direct exact job is self-referential")
            if job.member_key in job_members:
                raise GroupFirstError("a group member has more than one exact job")
            job_members.add(job.member_key)
            group = group_by_key.get(job.member_key)
            if group is None or job.master_key not in group.all_keys:
                raise GroupFirstError("exact job crosses group boundary")
            if group.uv_area_master_key != job.master_key:
                raise GroupFirstError("exact job does not use the UV-area master")
        expected_bound = len(self.islands) - len(self.groups)
        if self.exact_job_bound != expected_bound:
            raise GroupFirstError("exact_job_bound is inconsistent with group count")


@dataclass
class _MutableGroup:
    bucket_key: Any
    representative: IslandRecord
    members: list[tuple[IslandRecord, GroupPairResult]]


def _coerce_island(value: Any, *, ordinal: int) -> IslandRecord:
    if isinstance(value, IslandRecord):
        if value.ordinal == 0 and ordinal:
            return IslandRecord(
                key=value.key,
                density=value.density,
                bucket_key=value.bucket_key,
                ordinal=ordinal,
                descriptor_digest=value.descriptor_digest,
                metadata=value.metadata,
                uv_area=value.uv_area,
                uv_size=value.uv_size,
            )
        return value
    key = _get(value, "key", "face_key", "island_key")
    if key is None:
        raise GroupFirstError("island record has no key")
    return IslandRecord(
        key=key,
        density=_get(value, "density", "texel_density", default=None),
        bucket_key=_get(value, "bucket_key", "structural_bucket", "bucket", default=()),
        ordinal=_get(value, "ordinal", "index", default=ordinal),
        descriptor_digest=_get(value, "descriptor_digest", default="") or "",
        metadata=_get(value, "metadata", default=()),
        uv_area=_get(value, "uv_area", "uv_size", default=None),
    )


def _coerce_result(value: Any) -> GroupPairResult:
    if isinstance(value, GroupPairResult):
        return value
    if isinstance(value, Mapping):
        return GroupPairResult(
            pair_ordinal=_get(value, "pair_ordinal", "ordinal", default=None),
            representative_key=_get(value, "representative_key", "master_key", default=None),
            candidate_key=_get(value, "candidate_key", "member_key", default=None),
            accepted=_get(value, "accepted", default=False),
            score=_get(value, "score", default=math.inf),
            reason=_get(value, "reason", default="") or "",
            transform=_get(value, "transform", default=None),
            evidence=_get(value, "evidence", "diagnostics", default=()),
        )
    return GroupPairResult(
        pair_ordinal=_get(value, "pair_ordinal", "ordinal", default=None),
        representative_key=_get(value, "representative_key", "master_key", default=None),
        candidate_key=_get(value, "candidate_key", "member_key", default=None),
        accepted=_get(value, "accepted", default=False),
        score=_get(value, "score", default=math.inf),
        reason=_get(value, "reason", default="") or "",
        transform=_get(value, "transform", default=None),
        evidence=_get(value, "evidence", "diagnostics", default=()),
    )


def _quality_accepts(result: GroupPairResult, tolerance: float) -> bool:
    if not result.accepted:
        return False
    if not math.isfinite(result.score):
        return False
    return result.score <= tolerance


def _density_master(
    records: Sequence[IslandRecord],
    tie_epsilon: float,
) -> tuple[Optional[IslandRecord], Optional[float]]:
    valid = [record for record in records if record.density is not None]
    if not valid:
        return None, None
    best = sorted(valid, key=lambda item: _key_token(item.key))[0]
    for record in sorted(valid, key=lambda item: _key_token(item.key)):
        assert record.density is not None
        assert best.density is not None
        if record.density > best.density + tie_epsilon:
            best = record
        elif abs(record.density - best.density) <= tie_epsilon and _key_token(record.key) < _key_token(best.key):
            best = record
    return best, best.density


def _uv_area_master(
    records: Sequence[IslandRecord],
    tie_epsilon: float,
) -> tuple[Optional[IslandRecord], Optional[float]]:
    """Choose the maximum visible UV area, with a stable scale-aware tie."""

    valid = [record for record in records if record.uv_area is not None]
    if not valid:
        return None, None
    best = sorted(valid, key=lambda item: _key_token(item.key))[0]
    for record in sorted(valid, key=lambda item: _key_token(item.key)):
        assert record.uv_area is not None
        assert best.uv_area is not None
        scale = max(1.0, abs(record.uv_area), abs(best.uv_area))
        difference = record.uv_area - best.uv_area
        if difference > tie_epsilon * scale:
            best = record
        elif abs(difference) <= tie_epsilon * scale and _key_token(record.key) < _key_token(best.key):
            best = record
    return best, best.uv_area


class GroupFirstFrontier:
    """Incremental Normal-equivalent fixed-representative group frontier.

    Constructing the frontier emits the first representative's requests.
    Callers may submit those requests to any worker and feed results back via
    :meth:`consume`; results that arrive out of order are buffered.  The
    frontier never lets a later item affect an earlier group decision.
    """

    def __init__(
        self,
        islands: Iterable[Any],
        *,
        similarity_tolerance: float = float("inf"),
        density_tie_epsilon: float = DEFAULT_DENSITY_TIE_EPSILON,
        uv_area_tie_epsilon: float = DEFAULT_UV_AREA_TIE_EPSILON,
    ) -> None:
        try:
            tolerance = float(similarity_tolerance)
        except (TypeError, ValueError):
            raise GroupFirstError("similarity_tolerance must be numeric")
        if math.isnan(tolerance) or tolerance < 0.0:
            raise GroupFirstError("similarity_tolerance must be non-negative")
        try:
            tie_epsilon = float(density_tie_epsilon)
        except (TypeError, ValueError):
            raise GroupFirstError("density_tie_epsilon must be numeric")
        if not math.isfinite(tie_epsilon) or tie_epsilon < 0.0:
            raise GroupFirstError("density_tie_epsilon must be finite and non-negative")
        records = tuple(
            _coerce_island(value, ordinal=index)
            for index, value in enumerate(tuple(islands))
        )
        if len({item.key for item in records}) != len(records):
            raise GroupFirstError("island keys must be unique")
        self.islands = tuple(
            sorted(
                records,
                key=lambda item: (
                    _stable_token(item.bucket_key),
                    _key_token(item.key),
                    item.ordinal,
                ),
            )
        )
        self.similarity_tolerance = tolerance
        self.density_tie_epsilon = tie_epsilon
        try:
            area_tie_epsilon = float(uv_area_tie_epsilon)
        except (TypeError, ValueError):
            raise GroupFirstError("uv_area_tie_epsilon must be numeric")
        if not math.isfinite(area_tie_epsilon) or area_tie_epsilon < 0.0:
            raise GroupFirstError("uv_area_tie_epsilon must be finite and non-negative")
        self.uv_area_tie_epsilon = area_tie_epsilon
        self._cursor = 0
        self._active_bucket: Any = None
        self._active_groups: list[_MutableGroup] = []
        self._groups: list[_MutableGroup] = []
        self._generated: dict[tuple[tuple[Any, ...], tuple[Any, ...]], GroupPairRequest] = {}
        self._consumed: dict[tuple[tuple[Any, ...], tuple[Any, ...]], GroupPairResult] = {}
        self._groups_started = False
        self._position_by_key = {
            record.key: index for index, record in enumerate(self.islands)
        }
        self._finalized: Optional[GroupFirstPlan] = None
        # GroupRecord content depends only on the immutable island records and
        # the membership/results already appended to each mutable group.  The
        # cursor and comparison counters are progress telemetry and cannot
        # change that semantic view, so keep one immutable view per membership
        # epoch and invalidate it only on a group/member append.
        self._groups_membership_epoch = 0
        self._groups_view_cache_epoch = -1
        self._groups_view_cache: Optional[tuple[GroupRecord, ...]] = None
        self.comparisons_planned = 0
        self.comparisons_completed = 0
        self.accepted_comparisons = 0
        self.rejected_comparisons = 0
        self._drain()

    @property
    def complete(self) -> bool:
        return self._finalized is not None

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def groups(self) -> tuple[GroupRecord, ...]:
        if self._finalized is not None:
            return self._finalized.groups
        return self._frozen_groups()

    @property
    def pending(self) -> tuple[GroupPairRequest, ...]:
        return self.pending_requests()

    def pending_requests(self) -> tuple[GroupPairRequest, ...]:
        return tuple(
            request
            for pair_key, request in sorted(
                self._generated.items(), key=lambda item: item[1].pair_ordinal
            )
            if pair_key not in self._consumed
        )

    def all_requests(self) -> tuple[GroupPairRequest, ...]:
        return tuple(
            request
            for request in sorted(self._generated.values(), key=lambda item: item.pair_ordinal)
        )

    def _schedule_representative(self, representative: IslandRecord) -> None:
        representative_position = self._position_by_key[representative.key]
        for candidate in self.islands:
            if self._position_by_key[candidate.key] <= representative_position:
                continue
            if candidate.bucket_key != representative.bucket_key:
                continue
            pair_key = (representative.key, candidate.key)
            if pair_key in self._generated:
                continue
            request = GroupPairRequest(
                pair_ordinal=len(self._generated),
                representative_key=representative.key,
                candidate_key=candidate.key,
                bucket_key=representative.bucket_key,
            )
            self._generated[pair_key] = request
            self.comparisons_planned += 1

    def _start_group(self, record: IslandRecord) -> None:
        group = _MutableGroup(record.bucket_key, record, [])
        self._groups.append(group)
        self._groups_membership_epoch += 1
        self._groups_view_cache = None
        self._groups_view_cache_epoch = -1
        self._active_groups.append(group)
        self._groups_started = True
        self._schedule_representative(record)

    def _result_for(self, representative: IslandRecord, candidate: IslandRecord) -> Optional[GroupPairResult]:
        return self._consumed.get((representative.key, candidate.key))

    def _drain(self) -> None:
        while self._cursor < len(self.islands):
            candidate = self.islands[self._cursor]
            if self._active_bucket != candidate.bucket_key:
                self._active_bucket = candidate.bucket_key
                self._active_groups = []
                self._start_group(candidate)
                self._cursor += 1
                continue

            decisions = []
            missing = False
            for group in self._active_groups:
                result = self._result_for(group.representative, candidate)
                if result is None:
                    missing = True
                    break
                if _quality_accepts(result, self.similarity_tolerance):
                    decisions.append(
                        (
                            result.score,
                            _key_token(group.representative.key),
                            group,
                            result,
                        )
                    )
            if missing:
                return
            if decisions:
                decisions.sort(key=lambda item: (item[0], item[1]))
                _score, _representative_token, group, result = decisions[0]
                group.members.append((candidate, result))
                self._groups_membership_epoch += 1
                self._groups_view_cache = None
                self._groups_view_cache_epoch = -1
                self.accepted_comparisons += 1
            else:
                # Every existing representative has a completed rejection or
                # an out-of-tolerance result for this candidate.
                self._start_group(candidate)
                self.rejected_comparisons += len(self._active_groups) - 1
            self._cursor += 1

        if self._cursor == len(self.islands) and not self.pending_requests():
            self._finalized = self._build_plan()

    def consume(self, value: Any, result_value: Any = None) -> tuple[GroupPairRequest, ...]:
        """Consume one result and return newly generated request objects."""

        if result_value is not None:
            if isinstance(value, GroupPairRequest):
                result = GroupPairResult.from_match_result(value, result_value)
            else:
                result = _coerce_result(result_value)
        else:
            result = _coerce_result(value)
        pair_key = result.pair_key
        request = self._generated.get(pair_key)
        if request is None:
            raise UnknownPairResultError(
                "unrequested group pair %r -> %r"
                % (result.representative_key, result.candidate_key)
            )
        if request.pair_ordinal != result.pair_ordinal:
            raise GroupFirstError("pair result ordinal does not match request")
        previous = self._consumed.get(pair_key)
        if previous is not None:
            if previous != result:
                raise ConflictingPairResultError("conflicting result for %r" % (pair_key,))
            return ()
        self._consumed[pair_key] = result
        self.comparisons_completed += 1
        self._drain()
        return self.pending_requests()

    def consume_many(self, values: Iterable[Any]) -> tuple[GroupPairRequest, ...]:
        for value in values:
            self.consume(value)
        return self.pending_requests()

    consume_result = consume
    add_result = consume

    def _freeze_group(self, index: int, group: _MutableGroup) -> GroupRecord:
        all_records = (group.representative,) + tuple(item for item, _result in group.members)
        density_master, density = _density_master(all_records, self.density_tie_epsilon)
        uv_area_master, uv_area = _uv_area_master(all_records, self.uv_area_tie_epsilon)
        return GroupRecord(
            group_index=index,
            bucket_key=group.bucket_key,
            representative_key=group.representative.key,
            member_keys=tuple(item.key for item, _result in group.members),
            density_master_key=density_master.key if density_master is not None else None,
            density_master_density=density,
            member_shape_pair_ordinals=tuple(result.pair_ordinal for _item, result in group.members),
            uv_area_master_key=uv_area_master.key if uv_area_master is not None else None,
            uv_area_master_area=uv_area,
        )

    def _frozen_groups(self) -> tuple[GroupRecord, ...]:
        """Return the immutable group view for the current membership epoch."""

        if self._finalized is not None:
            return self._finalized.groups
        if (
            self._groups_view_cache is not None
            and self._groups_view_cache_epoch == self._groups_membership_epoch
        ):
            return self._groups_view_cache
        groups = tuple(
            self._freeze_group(index, group)
            for index, group in enumerate(self._groups)
        )
        self._groups_view_cache = groups
        self._groups_view_cache_epoch = self._groups_membership_epoch
        return groups

    def _build_plan(self) -> GroupFirstPlan:
        groups = self._frozen_groups()
        records_by_key = {item.key: item for item in self.islands}
        jobs = []
        for group in groups:
            if group.uv_area_master_key is None or group.size < 2:
                continue
            master = records_by_key[group.uv_area_master_key]
            representative_key = group.representative_key
            accepted_members = self._groups[group.group_index].members

            def transform_for(item_key: tuple[Any, ...]):
                if item_key == representative_key:
                    # The fixed representative is the explicit identity leg.
                    return None, True, None
                result = next(
                    (
                        result
                        for item, result in accepted_members
                        if item.key == item_key
                    ),
                    None,
                )
                if result is None or result.transform is None:
                    return None, False, "missing_shape_transform"
                return result.transform, False, None

            for member_key in group.all_keys:
                if member_key == group.uv_area_master_key:
                    continue
                member = records_by_key[member_key]
                seed_transform = None
                seed_source = "missing"
                seed_missing_reason = None
                seed_identity_leg_used = False
                master_to_representative, master_identity, master_reason = transform_for(
                    group.uv_area_master_key
                )
                member_to_representative, member_identity, member_reason = transform_for(
                    member_key
                )
                seed_identity_leg_used = bool(master_identity or member_identity)
                if master_reason is not None:
                    seed_missing_reason = "master_%s" % master_reason
                elif member_reason is not None:
                    seed_missing_reason = "member_%s" % member_reason
                else:
                    seed_transform = _seed_algebra.reroot_member_to_master(
                        master_to_representative,
                        member_to_representative,
                    )
                    if seed_transform is None:
                        seed_missing_reason = "seed_reroot_failed"
                    elif not _validate_reroot_seed(
                        master_to_representative,
                        member_to_representative,
                        seed_transform,
                    ):
                        seed_transform = None
                        seed_missing_reason = "seed_re_root_validation_failed"
                    else:
                        seed_source = "direct" if master_identity else "rerooted"
                jobs.append(
                    DirectExactJob(
                        job_ordinal=len(jobs),
                        master_key=group.uv_area_master_key,
                        member_key=member_key,
                        group_index=group.group_index,
                        source_representative_key=group.representative_key,
                        shape_pair_ordinal=(
                            next(
                                result.pair_ordinal
                                for item, result in self._groups[group.group_index].members
                                if item.key == member_key
                            )
                            if member_key != group.representative_key
                            else None
                        ),
                        master_density=master.density,
                        member_density=member.density,
                        master_uv_area=master.uv_area,
                        member_uv_area=member.uv_area,
                        seed_transform=seed_transform,
                        seed_source=seed_source,
                        seed_missing_reason=seed_missing_reason,
                        seed_identity_leg_used=seed_identity_leg_used,
                    )
                )
        exact_jobs = tuple(jobs)
        membership_semantics = (
            SCHEMA_VERSION,
            ALGORITHM_VERSION,
            tuple(
                (
                    group.bucket_key,
                    group.representative_key,
                    group.all_keys,
                    group.uv_area_master_key,
                    group.uv_area_master_area,
                )
                for group in groups
            ),
        )
        job_semantics = tuple(
            (
                job.job_ordinal,
                job.master_key,
                job.member_key,
                job.group_index,
                job.master_uv_area,
                job.member_uv_area,
                job.seed_source,
                job.seed_missing_reason,
                job.seed_identity_leg_used,
                job.seed_transform,
            )
            for job in exact_jobs
        )
        plan = GroupFirstPlan(
            islands=self.islands,
            groups=groups,
            exact_jobs=exact_jobs,
            comparisons_planned=self.comparisons_planned,
            comparisons_completed=self.comparisons_completed,
            accepted_comparisons=self.accepted_comparisons,
            rejected_comparisons=self.rejected_comparisons,
            exact_job_bound=len(self.islands) - len(groups),
            membership_digest=stable_digest(membership_semantics),
            exact_jobs_digest=stable_digest((SCHEMA_VERSION, job_semantics)),
            uv_area_by_key=tuple((item.key, item.uv_area) for item in self.islands),
            uv_size_by_key=tuple((item.key, item.uv_size) for item in self.islands),
        )
        plan.validate()
        return plan

    def finalize(self) -> GroupFirstPlan:
        """Return the plan only after every requested shape result is terminal."""

        self._drain()
        if self._finalized is None:
            pending = self.pending_requests()
            raise IncompleteGroupPlanError(
                "group-first plan is incomplete: cursor=%d/%d pending=%d"
                % (self._cursor, len(self.islands), len(pending))
            )
        return self._finalized

    result = finalize


GroupFirstPlanner = GroupFirstFrontier
GroupFrontier = GroupFirstFrontier


def _result_index(values: Any) -> dict[Any, GroupPairResult]:
    if isinstance(values, Mapping):
        iterable = values.items()
    else:
        iterable = ((None, value) for value in values)
    indexed: dict[Any, GroupPairResult] = {}
    for supplied_key, value in iterable:
        if isinstance(value, GroupPairResult):
            result = value
        else:
            result = _coerce_result(value)
        indexed[result.pair_key] = result
        indexed[result.pair_ordinal] = result
        if supplied_key is not None:
            indexed[supplied_key] = result
    return indexed


def build_group_first_plan(
    islands: Iterable[Any],
    shape_results: Any,
    *,
    similarity_tolerance: float = float("inf"),
    density_tie_epsilon: float = DEFAULT_DENSITY_TIE_EPSILON,
    uv_area_tie_epsilon: float = DEFAULT_UV_AREA_TIE_EPSILON,
) -> GroupFirstPlan:
    """Build a complete plan from an out-of-order result collection.

    The result collection may be a mapping keyed by pair or ordinal, or any
    iterable of result records.  Only requests actually reached by the
    canonical frontier are consumed; this preserves the fixed-representative
    Normal semantics and avoids requiring a density cross-product.
    """

    frontier = GroupFirstFrontier(
        islands,
        similarity_tolerance=similarity_tolerance,
        density_tie_epsilon=density_tie_epsilon,
        uv_area_tie_epsilon=uv_area_tie_epsilon,
    )
    indexed = _result_index(shape_results)
    while not frontier.complete:
        pending = frontier.pending_requests()
        if not pending:
            return frontier.finalize()
        consumed = False
        # Deliver in reverse canonical order to make the helper explicitly
        # exercise out-of-order buffering while keeping deterministic output.
        for request in reversed(pending):
            result = indexed.get(request.pair_key)
            if result is None:
                result = indexed.get(request.pair_ordinal)
            if result is None:
                continue
            frontier.consume(result)
            consumed = True
        if not consumed:
            raise IncompleteGroupPlanError(
                "missing shape result for canonical request %r"
                % (pending[0].pair_key,)
            )
    return frontier.finalize()


plan_group_first = build_group_first_plan
make_group_first_plan = build_group_first_plan


__all__ = [
    "ALGORITHM_VERSION",
    "DEFAULT_DENSITY_TIE_EPSILON",
    "DEFAULT_UV_AREA_TIE_EPSILON",
    "SCHEMA_VERSION",
    "ConflictingPairResultError",
    "DirectExactJob",
    "GroupFirstError",
    "GroupFirstFrontier",
    "GroupFirstPlan",
    "GroupFirstPlanner",
    "GroupFrontier",
    "GroupMatchResult",
    "GroupPairRequest",
    "GroupPairResult",
    "GroupRecord",
    "IncompleteGroupPlanError",
    "IslandRecord",
    "PairRequest",
    "ShapePairRequest",
    "ShapePairResult",
    "UnknownPairResultError",
    "build_group_first_plan",
    "make_group_first_plan",
    "plan_group_first",
    "stable_digest",
]
