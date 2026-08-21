"""Finite, pure-Python algebra for candidate-to-master 2D similarity seeds.

The wire-level seed contract is the five-tuple::

    (angle, scale, reflected, source_center, target_center)

It represents the map ``target + scale * R(angle) * F(reflected) *
(point - source)``.  ``F(True)`` flips the x component before rotation;
``F(False)`` is the identity.  All public operations return immutable,
canonical tuples or ``None``.  ``None`` is therefore a safe result for an
invalid, non-finite, zero-scale, singular, or overflowing calculation.

This module intentionally has no Blender, BMesh, NumPy, or project imports.
It is suitable for the pure group-first and worker-boundary layers.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any, Optional, Tuple


Point2 = Tuple[float, float]
Seed = Tuple[float, float, bool, Point2, Point2]
# Affine coefficients are ``(m00, m01, m10, m11, tx, ty)`` and map
# ``(x, y)`` to ``(m00*x + m01*y + tx, m10*x + m11*y + ty)``.
Affine2D = Tuple[float, float, float, float, float, float]

_TWO_PI = 2.0 * math.pi
_MISSING = object()


def _finite_float(value: Any) -> Optional[float]:
    """Return a non-boolean finite float, or ``None`` on invalid input."""

    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    # A single representation for signed zero makes canonical tuples stable.
    return 0.0 if result == 0.0 else result


def _finite_point(value: Any) -> Optional[Point2]:
    """Copy a finite two-dimensional point without retaining mutable input."""

    try:
        if hasattr(value, "x") and hasattr(value, "y"):
            x = _finite_float(value.x)
            y = _finite_float(value.y)
        else:
            if isinstance(value, (str, bytes)):
                return None
            x = _finite_float(value[0])
            y = _finite_float(value[1])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    if x is None or y is None:
        return None
    return (x, y)


def _field(value: Any, *names: str) -> Any:
    """Read the first available seed field from a mapping or object."""

    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return _MISSING
    for name in names:
        try:
            return getattr(value, name)
        except AttributeError:
            continue
    return _MISSING


def _seed_parts(value: Any) -> Optional[Tuple[Any, Any, Any, Any, Any]]:
    """Extract the five primitive fields accepted by :func:`canonical_seed`."""

    if isinstance(value, (tuple, list)):
        if len(value) != 5:
            return None
        return (value[0], value[1], value[2], value[3], value[4])
    if value is None:
        return None
    angle = _field(value, "angle")
    scale = _field(value, "scale")
    reflected = _field(value, "reflected")
    source = _field(value, "source_center", "candidate_center")
    target = _field(value, "target_center", "reference_center")
    if _MISSING in (angle, scale, reflected, source, target):
        return None
    return (angle, scale, reflected, source, target)


def _canonical_angle(value: Any) -> Optional[float]:
    """Normalize a finite angle to the deterministic half-open range."""

    angle = _finite_float(value)
    if angle is None:
        return None
    # fmod avoids ``angle + pi`` overflowing for a large but finite input.
    result = math.fmod(angle, _TWO_PI)
    if result >= math.pi:
        result -= _TWO_PI
    elif result < -math.pi:
        result += _TWO_PI
    # Choose [-pi, pi), and collapse signed zero.
    if result >= math.pi:
        result = -math.pi
    return 0.0 if result == 0.0 else result


def canonical_seed(value: Any) -> Optional[Seed]:
    """Return a finite canonical seed tuple, or ``None`` if it is invalid.

    Accepted seed-like values are a five-item tuple/list, a mapping with the
    five contract names, or an object exposing those names.  Reflection is
    deliberately strict: only ``True`` or ``False`` is accepted.  The output
    always contains floats, a boolean reflection flag, and immutable point
    tuples.  Angles are normalized to ``[-pi, pi)``.
    """

    parts = _seed_parts(value)
    if parts is None:
        return None
    angle = _canonical_angle(parts[0])
    scale = _finite_float(parts[1])
    source = _finite_point(parts[3])
    target = _finite_point(parts[4])
    if angle is None or scale is None or scale <= 0.0:
        return None
    if not isinstance(parts[2], bool) or source is None or target is None:
        return None
    return (angle, scale, parts[2], source, target)


def identity_seed(center: Any = (0.0, 0.0)) -> Optional[Seed]:
    """Return an identity map centered at a finite point, or ``None``.

    The center is present only to retain the five-field seed representation;
    ``(0, 1, False, center, center)`` maps every point to itself.
    """

    point = _finite_point(center)
    if point is None:
        return None
    return (0.0, 1.0, False, point, point)


def _is_identity(seed: Seed) -> bool:
    return (
        seed[0] == 0.0
        and seed[1] == 1.0
        and not seed[2]
        and seed[3] == seed[4]
    )


def _apply_canonical(seed: Seed, point: Point2) -> Optional[Point2]:
    """Apply a validated seed without repeating canonicalization."""

    x = point[0] - seed[3][0]
    y = point[1] - seed[3][1]
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    if seed[2]:
        x = -x
    cosine = math.cos(seed[0])
    sine = math.sin(seed[0])
    rotated_x = cosine * x - sine * y
    rotated_y = sine * x + cosine * y
    if not math.isfinite(rotated_x) or not math.isfinite(rotated_y):
        return None
    scaled_x = seed[1] * rotated_x
    scaled_y = seed[1] * rotated_y
    if not math.isfinite(scaled_x) or not math.isfinite(scaled_y):
        return None
    result = (
        seed[4][0] + scaled_x,
        seed[4][1] + scaled_y,
    )
    if not math.isfinite(result[0]) or not math.isfinite(result[1]):
        return None
    return (0.0 if result[0] == 0.0 else result[0], 0.0 if result[1] == 0.0 else result[1])


def apply_seed(seed: Any, point: Any) -> Optional[Point2]:
    """Apply a seed to a finite point, returning a finite point or ``None``."""

    normalized = canonical_seed(seed)
    source_point = _finite_point(point)
    if normalized is None or source_point is None:
        return None
    return _apply_canonical(normalized, source_point)


def seed_to_affine(seed: Any) -> Optional[Affine2D]:
    """Convert a seed to ``(m00, m01, m10, m11, tx, ty)``, or ``None``.

    The returned affine map is equivalent to :func:`apply_seed` and has no
    center fields.  Overflow during coefficient or translation calculation is
    treated as invalid rather than emitting non-finite wire data.
    """

    normalized = canonical_seed(seed)
    if normalized is None:
        return None
    angle, scale, reflected, source, target = normalized
    cosine = math.cos(angle)
    sine = math.sin(angle)
    if reflected:
        m00 = -scale * cosine
        m01 = -scale * sine
        m10 = -scale * sine
        m11 = scale * cosine
    else:
        m00 = scale * cosine
        m01 = -scale * sine
        m10 = scale * sine
        m11 = scale * cosine
    if not all(math.isfinite(item) for item in (m00, m01, m10, m11)):
        return None
    tx = target[0] - (m00 * source[0] + m01 * source[1])
    ty = target[1] - (m10 * source[0] + m11 * source[1])
    result = (m00, m01, m10, m11, tx, ty)
    if not all(math.isfinite(item) for item in result):
        return None
    return tuple(0.0 if item == 0.0 else item for item in result)  # type: ignore[return-value]


def _affine_parts(value: Any) -> Optional[Affine2D]:
    if not isinstance(value, (tuple, list)) or len(value) != 6:
        return None
    values = tuple(_finite_float(item) for item in value)
    if any(item is None for item in values):
        return None
    return tuple(values)  # type: ignore[return-value]


def affine_to_seed(
    value: Any,
    source_center: Any = (0.0, 0.0),
    *,
    tolerance: float = 1.0e-9,
) -> Optional[Seed]:
    """Convert a finite similarity affine tuple back to a canonical seed.

    ``value`` uses the coefficient order documented by :data:`Affine2D`.
    The matrix must have equal column norms and orthogonal columns within the
    relative ``tolerance``; a zero or singular matrix returns ``None``.  The
    chosen source center is mapped through the affine translation to form the
    output target center.
    """

    affine = _affine_parts(value)
    source = _finite_point(source_center)
    tol = _finite_float(tolerance)
    if affine is None or source is None or tol is None or tol < 0.0:
        return None
    m00, m01, m10, m11, tx, ty = affine
    scale = math.hypot(m00, m10)
    other_scale = math.hypot(m01, m11)
    if not math.isfinite(scale) or not math.isfinite(other_scale) or scale <= 0.0:
        return None
    # Normalize before checking similarity, avoiding overflow in determinant
    # or squared-norm products for large but finite matrix coefficients.
    u = m00 / scale
    v = m10 / scale
    w = m01 / scale
    z = m11 / scale
    normalized_other_scale = math.hypot(w, z)
    dot = u * w + v * z
    determinant = u * z - w * v
    if not all(math.isfinite(item) for item in (u, v, w, z, normalized_other_scale, dot, determinant)):
        return None
    limit = tol * max(1.0, normalized_other_scale)
    if abs(other_scale / scale - 1.0) > limit:
        return None
    if abs(dot) > limit or abs(abs(determinant) - 1.0) > limit:
        return None
    if abs(determinant) <= tol:
        return None
    reflected = determinant < 0.0
    if reflected:
        angle = math.atan2(-v, -u)
    else:
        angle = math.atan2(v, u)
    target_x = m00 * source[0] + m01 * source[1] + tx
    target_y = m10 * source[0] + m11 * source[1] + ty
    if not math.isfinite(target_x) or not math.isfinite(target_y):
        return None
    return canonical_seed((angle, scale, reflected, source, (target_x, target_y)))


def inverse_seed(seed: Any) -> Optional[Seed]:
    """Return the finite inverse of a seed, or ``None`` if unavailable."""

    normalized = canonical_seed(seed)
    if normalized is None:
        return None
    if _is_identity(normalized):
        return normalized
    angle, scale, reflected, source, target = normalized
    inverse_scale = 1.0 / scale
    if not math.isfinite(inverse_scale):
        return None
    inverse_angle = angle if reflected else -angle
    return canonical_seed((inverse_angle, inverse_scale, reflected, target, source))


def compose_seeds(outer: Any, inner: Any) -> Optional[Seed]:
    """Return ``outer ∘ inner`` as one canonical seed, or ``None``.

    ``inner`` is applied first.  If the inputs use ``(a1, s1, f1)`` and
    ``(a2, s2, f2)`` for inner and outer respectively, the resulting linear
    parameters are ``scale=s2*s1``, ``reflected=f2 xor f1``, and
    ``angle=a2 + (-a1 if f2 else a1)``.  The source center is the inner source
    center; the target center is the composed image of that source center.
    """

    normalized_outer = canonical_seed(outer)
    normalized_inner = canonical_seed(inner)
    if normalized_outer is None or normalized_inner is None:
        return None
    if _is_identity(normalized_outer):
        return normalized_inner
    if _is_identity(normalized_inner):
        return normalized_outer
    outer_angle, outer_scale, outer_reflected, _outer_source, _outer_target = normalized_outer
    inner_angle, inner_scale, inner_reflected, inner_source, inner_target = normalized_inner
    scale = outer_scale * inner_scale
    angle = outer_angle + (-inner_angle if outer_reflected else inner_angle)
    if not math.isfinite(scale) or scale <= 0.0 or not math.isfinite(angle):
        return None
    target = _apply_canonical(normalized_outer, inner_target)
    if target is None:
        return None
    return canonical_seed(
        (angle, scale, outer_reflected != inner_reflected, inner_source, target)
    )


def reroot_member_to_master(
    master_to_representative: Any,
    member_to_representative: Any,
) -> Optional[Seed]:
    """Re-root a member-to-representative seed at the UV-area master.

    The exact relation is::

        member_to_master = inverse(master_to_representative)
                           composed with member_to_representative

    ``None`` is interpreted as an explicit identity representative seed.  This
    handles both identity cases safely: a representative that is the master
    (first argument ``None``) and a member that is the representative (second
    argument ``None``).  Non-``None`` invalid inputs still return ``None``.
    """

    master_seed = identity_seed() if master_to_representative is None else canonical_seed(master_to_representative)
    member_seed = identity_seed() if member_to_representative is None else canonical_seed(member_to_representative)
    if master_seed is None or member_seed is None:
        return None
    inverse_master = inverse_seed(master_seed)
    if inverse_master is None:
        return None
    return compose_seeds(inverse_master, member_seed)


__all__ = [
    "Affine2D",
    "Point2",
    "Seed",
    "affine_to_seed",
    "apply_seed",
    "canonical_seed",
    "compose_seeds",
    "identity_seed",
    "inverse_seed",
    "reroot_member_to_master",
    "seed_to_affine",
]
