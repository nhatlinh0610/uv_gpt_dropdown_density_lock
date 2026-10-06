"""Pure planning for force-snapping selected UV islands by exact topology.

The planner deliberately ignores UV-shape residual as an acceptance gate.  UV
coordinates are used only to rank masters by area and to disambiguate symmetric
topology mappings; a complete topological correspondence is accepted even when
the candidate island has been skewed or edited in UV space.
"""

from dataclasses import dataclass
import math
from typing import Any, Tuple

from . import topology_correspondence


_FORCE_RESIDUAL_TOLERANCE = 1.0e300


@dataclass(frozen=True)
class SnapIslandRecord:
    key: Any
    uv_area: float
    graph: topology_correspondence.IslandGraph


@dataclass(frozen=True)
class SnapPair:
    candidate_key: Any
    master_key: Any
    correspondence: topology_correspondence.CorrespondenceResult


@dataclass(frozen=True)
class ForceSnapPlan:
    pairs: Tuple[SnapPair, ...]
    master_keys: Tuple[Any, ...]
    skipped_keys: Tuple[Any, ...] = ()

    @property
    def group_count(self):
        return len(self.master_keys)


def _stable_key(value):
    return (type(value).__name__, repr(value))


def _finite_area(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def topology_bucket_key(graph):
    """Return a UV-shape-independent structural prefilter for exact matching."""

    return (
        len(graph.faces),
        len(graph.edges),
        len(graph.vertices),
        len(graph.loops),
        len(graph.boundaries),
        tuple(sorted(len(face.loop_keys) for face in graph.faces)),
        tuple(sorted((len(edge.loop_keys), len(edge.face_keys)) for edge in graph.edges)),
        tuple(sorted(len(vertex.loop_keys) for vertex in graph.vertices)),
        tuple(sorted((boundary.role, len(boundary.loop_keys)) for boundary in graph.boundaries)),
    )


def plan_force_snap(records, *, max_search=100000):
    """Group compatible topology and force-map each target to the largest master.

    Within each structural bucket, the largest valid UV-area island is attempted
    first as master. Exact correspondence runs with an effectively-unbounded
    finite residual tolerance, so geometric UV distortion does not reject an
    otherwise complete topology
    mapping. Reflection is always allowed because the final operation copies
    master UV coordinates exactly; reflection only helps identify correspondence.
    """

    valid = []
    skipped = []
    for record in records:
        area = _finite_area(record.uv_area)
        if area is None:
            skipped.append(record.key)
            continue
        valid.append((record, area))

    valid.sort(key=lambda item: (-item[1], _stable_key(item[0].key)))
    remaining = [item[0] for item in valid]
    pairs = []
    masters = []

    while remaining:
        master = remaining.pop(0)
        masters.append(master.key)
        master_bucket = topology_bucket_key(master.graph)
        keep = []
        for candidate in remaining:
            if topology_bucket_key(candidate.graph) != master_bucket:
                keep.append(candidate)
                continue
            result = topology_correspondence.find_correspondence(
                master.graph,
                candidate.graph,
                allow_flipping=True,
                match_scale=True,
                tolerance=_FORCE_RESIDUAL_TOLERANCE,
                max_search=max_search,
            )
            if result.accepted:
                pairs.append(
                    SnapPair(
                        candidate_key=candidate.key,
                        master_key=master.key,
                        correspondence=result,
                    )
                )
            else:
                keep.append(candidate)
        remaining = keep

    return ForceSnapPlan(
        pairs=tuple(pairs),
        master_keys=tuple(masters),
        skipped_keys=tuple(sorted(skipped, key=_stable_key)),
    )


__all__ = [
    "ForceSnapPlan",
    "SnapIslandRecord",
    "SnapPair",
    "plan_force_snap",
    "topology_bucket_key",
]
