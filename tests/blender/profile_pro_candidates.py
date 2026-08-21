"""P01 read-only Blender profile for the bounded Pro candidate planner.

The command line opens the exact locked fixture before this script runs.  This
script copies only immutable graph/signature/density records, streams planner
batches, and never enters Edit Mode, updates a mesh, runs an operator, or saves
the blend file.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
import time
import traceback
import tracemalloc


sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "PERF-P01"
DEFAULT_FIXTURE = Path(r"C:\Users\linhp\Downloads\cc.blend")
EXPECTED_FIXTURE_SHA = (
    "558CE02B2B36394A528290B38B7E5FE072B5853EAEB7EBAB71E515EDC6C5E905"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 577


class ProfileError(RuntimeError):
    pass


def _arg_value(name, default):
    if "--" not in sys.argv:
        return default
    args = sys.argv[sys.argv.index("--") + 1 :]
    try:
        index = args.index(name)
    except ValueError:
        return default
    if index + 1 >= len(args):
        return default
    return args[index + 1]


def _has_flag(name):
    if "--" not in sys.argv:
        return False
    return name in sys.argv[sys.argv.index("--") + 1 :]


SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = Path(
    _arg_value("--project-root", str(SCRIPT_PATH.parents[2]))
).resolve()
FIXTURE_PATH = Path(_arg_value("--fixture", str(DEFAULT_FIXTURE))).resolve()
EXPECTED_SHA = _arg_value("--expected-sha", EXPECTED_FIXTURE_SHA).upper()
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_01_candidate_profile.json"),
    )
).resolve()
TRACEMALLOC_ENABLED = not _has_flag("--no-tracemalloc")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _face_key(island):
    return tuple(sorted({int(loop.face.index) for loop in island}))


def _cheap_bucket_key(signature):
    return (
        int(signature.component_count),
        int(signature.closed_component_count),
        int(signature.open_component_count),
        int(signature.ambiguous_component_count),
        int(signature.degenerate_segment_count),
        int(signature.cycle_count),
        signature.raw_boundary_signature,
        tuple(getattr(getattr(signature, "topology", None), "core_key", ()) or ()),
    )


def _profile_graph_for_island(island, uv_layer, topology_correspondence):
    """Copy incidence/cycle topology without running the Pro boundary tracer."""

    island_loop_ids = {id(loop) for loop in island}
    faces_by_key = {}
    for loop in island:
        faces_by_key[int(loop.face.index)] = loop.face
    faces = tuple(sorted(faces_by_key.values(), key=lambda face: int(face.index)))
    if not faces:
        raise ProfileError("island has no faces")

    loop_by_key = {}
    face_loop_keys = {}
    edge_loops = {}
    vertex_loops = {}
    for face in faces:
        keys = []
        for local_index, loop in enumerate(face.loops):
            if id(loop) not in island_loop_ids:
                raise ProfileError("island contains a partial face")
            key = (int(face.index), int(local_index))
            loop_by_key[key] = loop
            keys.append(key)
            edge_loops.setdefault(int(loop.edge.index), []).append(key)
            vertex_loops.setdefault(int(loop.vert.index), []).append(key)
        face_loop_keys[int(face.index)] = tuple(keys)

    vertex_uvs = {}
    for key, loop in loop_by_key.items():
        uv = loop[uv_layer].uv
        vertex_uvs.setdefault(int(loop.vert.index), set()).add(
            (round(float(uv.x), 10), round(float(uv.y), 10))
        )
    uv_split = {
        vertex_key: len(values) > 1 for vertex_key, values in vertex_uvs.items()
    }

    edge_face_keys = {}
    edge_boundary = {}
    edge_non_manifold = {}
    for edge_key, keys in edge_loops.items():
        face_keys = tuple(
            sorted({int(loop_by_key[key].face.index) for key in keys})
        )
        edge_face_keys[edge_key] = face_keys
        edge_boundary[edge_key] = len(face_keys) == 1
        representative = loop_by_key[keys[0]].edge
        edge_non_manifold[edge_key] = sum(
            1 for _face in getattr(representative, "link_faces", ())
        ) > 2

    loop_records = []
    for key, loop in sorted(loop_by_key.items()):
        face_key, local_index = key
        cycle = face_loop_keys[face_key]
        edge_key = int(loop.edge.index)
        vertex_key = int(loop.vert.index)
        uv = loop[uv_layer].uv
        loop_records.append(
            topology_correspondence.LoopRecord(
                key=key,
                face_key=face_key,
                edge_key=edge_key,
                vertex_key=vertex_key,
                next_key=cycle[(local_index + 1) % len(cycle)],
                prev_key=cycle[(local_index - 1) % len(cycle)],
                uv=(float(uv.x), float(uv.y)),
                boundary=edge_boundary[edge_key],
                seam=uv_split[vertex_key],
                signature=("uv_split", uv_split[vertex_key]),
            )
        )
    face_records = tuple(
        topology_correspondence.FaceRecord(
            key=face_key,
            loop_keys=face_loop_keys[face_key],
        )
        for face_key in sorted(face_loop_keys)
    )
    edge_records = tuple(
        topology_correspondence.EdgeRecord(
            key=edge_key,
            loop_keys=tuple(sorted(keys)),
            face_keys=edge_face_keys[edge_key],
            boundary=edge_boundary[edge_key],
            non_manifold=edge_non_manifold[edge_key],
            signature=("mesh_non_manifold", edge_non_manifold[edge_key]),
        )
        for edge_key, keys in sorted(edge_loops.items())
    )
    vertex_records = tuple(
        topology_correspondence.VertexRecord(
            key=vertex_key,
            loop_keys=tuple(sorted(keys)),
            boundary=any(
                edge_boundary[int(loop_by_key[key].edge.index)] for key in keys
            ),
            signature=("uv_split", uv_split[vertex_key]),
        )
        for vertex_key, keys in sorted(vertex_loops.items())
    )
    return topology_correspondence.make_graph(
        faces=face_records,
        edges=edge_records,
        vertices=vertex_records,
        loops=tuple(loop_records),
        boundaries=(),
    )


def _make_record(island, obj, uv_layer, stack_tools, similarity_matcher, planner):
    face_key = _face_key(island)
    from uv_gpt import topology_correspondence

    graph = _profile_graph_for_island(island, uv_layer, topology_correspondence)
    strict_fingerprint = planner.canonical_graph_color_signature(graph)
    segments, topology = stack_tools._numeric_island_inputs(island, uv_layer)
    cheap = similarity_matcher.build_cheap_signature(
        segments,
        face_key=face_key,
        topology=topology,
        include_invariants=True,
    )
    cheap_signature = (
        int(cheap.component_count),
        int(cheap.closed_component_count),
        int(cheap.open_component_count),
        int(cheap.ambiguous_component_count),
        int(cheap.degenerate_segment_count),
        int(cheap.cycle_count),
        tuple(float(value) for value in cheap.invariant_signature),
        cheap.raw_boundary_signature,
        tuple(getattr(getattr(cheap, "topology", None), "core_key", ()) or ()),
    )
    return planner.IslandRecord(
        face_key=face_key,
        strict_topology_fingerprint=strict_fingerprint,
        normalized_boundary_descriptor=tuple(
            float(value) for value in cheap.invariant_signature
        ),
        density=stack_tools._pro_density_for_island(obj, island, uv_layer),
        cheap_signature=cheap_signature,
    ), _cheap_bucket_key(cheap)


def _clean(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(item) for item in value]
    return str(value)


def _diagnostic_summary(diagnostics):
    def compact_face_key(key):
        if len(key) <= 8:
            return list(key)
        return {
            "count": len(key),
            "head": list(key[:4]),
            "tail": list(key[-4:]),
        }

    def compact_bucket_key(key):
        text = repr(key)
        if len(text) <= 96:
            return text
        return {
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:20],
            "repr_length": len(text),
        }

    return {
        "selected": diagnostics.selected,
        "topology_buckets": diagnostics.topology_buckets,
        "candidate_pairs": diagnostics.candidate_pairs,
        "theoretical_all_pairs": diagnostics.theoretical_all_pairs,
        "avoided_all_pairs": diagnostics.avoided_all_pairs,
        "truncated_member_count": len(diagnostics.truncated_members),
        "truncated_member_sample": [
            compact_face_key(key) for key in diagnostics.truncated_members[:12]
        ],
        "truncated_bucket_count": len(diagnostics.truncated_buckets),
        "truncated_bucket_sample": [
            compact_bucket_key(key) for key in diagnostics.truncated_buckets[:4]
        ],
        "max_bucket": diagnostics.max_bucket,
        "estimated_bytes": diagnostics.estimated_bytes,
        "elapsed_ms": diagnostics.elapsed_ms,
        "unresolved_members": diagnostics.unresolved_members,
        "reason_counts": [
            [str(reason), int(count)]
            for reason, count in diagnostics.reason_counts
        ],
    }


def main():
    fixture_sha_before = _sha256_file(FIXTURE_PATH)
    if fixture_sha_before != EXPECTED_SHA:
        raise ProfileError(
            "Fixture SHA before profile mismatch: %s != %s"
            % (fixture_sha_before, EXPECTED_SHA)
        )

    if bpy.data.filepath and Path(bpy.data.filepath).resolve() != FIXTURE_PATH:
        raise ProfileError(
            "Blender opened %s instead of %s"
            % (Path(bpy.data.filepath).resolve(), FIXTURE_PATH)
        )
    obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
    if obj is None or obj.type != "MESH":
        raise ProfileError("Target mesh object is missing: %s" % TARGET_OBJECT_NAME)

    from uv_gpt import pro_candidate_planner as planner
    from uv_gpt import similarity_matcher, stack_tools

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        bm.edges.ensure_lookup_table()
        bm.edges.index_update()
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
        if uv_layer is None:
            raise ProfileError("Target UV layer is missing: %s" % TARGET_UV_NAME)

        from uv_gpt import island_tools

        islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        if len(islands) != EXPECTED_ISLAND_COUNT:
            raise ProfileError(
                "Unexpected island count: %s != %s"
                % (len(islands), EXPECTED_ISLAND_COUNT)
            )

        if TRACEMALLOC_ENABLED:
            tracemalloc.start()
        record_started = time.perf_counter()
        records = []
        cheap_buckets = {}
        for island in islands:
            record, cheap_key = _make_record(
                island, obj, uv_layer, stack_tools, similarity_matcher, planner
            )
            records.append(record)
            cheap_buckets.setdefault(cheap_key, 0)
            cheap_buckets[cheap_key] += 1
        record_elapsed_ms = (time.perf_counter() - record_started) * 1000.0
        record_peak_bytes = (
            tracemalloc.get_traced_memory()[1] if TRACEMALLOC_ENABLED else None
        )

        config = planner.PlannerConfig(
            per_member_k=8,
            global_pair_budget=4096,
            per_bucket_pair_budget=4096,
            descriptor_bin_width=0.05,
            index_dimensions=2,
            fallback_probe_limit=16,
            fallback_candidate_limit=8,
            batch_size=256,
        )
        plan_started = time.perf_counter()
        candidate_sample = []
        streamed_pairs = 0
        batch_count = 0
        plan = planner.plan_candidates(records, config)
        for batch in plan.iter_batches():
            batch_count += 1
            streamed_pairs += len(batch)
            if len(candidate_sample) < 12:
                candidate_sample.extend(
                    {
                        "member_key": list(pair.member_key),
                        "master_key": list(pair.master_key),
                        "rank": pair.rank,
                        "tier": pair.tier,
                    }
                    for pair in batch[: 12 - len(candidate_sample)]
                )
        plan_elapsed_ms = (time.perf_counter() - plan_started) * 1000.0
        diagnostics = plan.diagnostics
        peak_memory_bytes = (
            tracemalloc.get_traced_memory()[1] if TRACEMALLOC_ENABLED else None
        )
        if TRACEMALLOC_ENABLED:
            tracemalloc.stop()

        if streamed_pairs != diagnostics.candidate_pairs:
            raise ProfileError(
                "Stream count disagrees with planner diagnostics: %s != %s"
                % (streamed_pairs, diagnostics.candidate_pairs)
            )
        old_bucket_pairs = sum(
            size * (size - 1) // 2 for size in cheap_buckets.values()
        )
        total_pairs = len(records) * (len(records) - 1) // 2
        fixture_sha_after = _sha256_file(FIXTURE_PATH)
        if fixture_sha_after != fixture_sha_before:
            raise ProfileError(
                "Fixture SHA changed during read-only profile: %s != %s"
                % (fixture_sha_before, fixture_sha_after)
            )

        return {
            "status": "passed",
            "packet_id": PACKET_ID,
            "fixture": str(FIXTURE_PATH),
            "fixture_sha_before": fixture_sha_before,
            "fixture_sha_after": fixture_sha_after,
            "object": TARGET_OBJECT_NAME,
            "uv_map": TARGET_UV_NAME,
            "island_count": len(records),
            "valid_density_count": sum(record.has_valid_density for record in records),
            "cheap_bucket_count": len(cheap_buckets),
            "old_theoretical_all_pairs": old_bucket_pairs,
            "all_island_pairs": total_pairs,
            "planned_candidate_pairs": diagnostics.candidate_pairs,
            "avoided_old_bucket_pairs": max(0, old_bucket_pairs - diagnostics.candidate_pairs),
            "avoided_all_island_pairs": max(0, total_pairs - diagnostics.candidate_pairs),
            "candidate_batches": batch_count,
            "candidate_sample": candidate_sample,
            "planner_config": {
                "per_member_k": config.per_member_k,
                "global_pair_budget": config.global_pair_budget,
                "per_bucket_pair_budget": config.per_bucket_pair_budget,
                "descriptor_bin_width": config.descriptor_bin_width,
                "index_dimensions": config.index_dimensions,
                "fallback_probe_limit": config.fallback_probe_limit,
                "fallback_candidate_limit": config.fallback_candidate_limit,
            },
            "planner_diagnostics": _diagnostic_summary(diagnostics),
            "record_build_elapsed_ms": record_elapsed_ms,
            "planner_elapsed_ms": plan_elapsed_ms,
            "planner_estimated_bytes": diagnostics.estimated_bytes,
            "record_build_peak_memory_bytes": record_peak_bytes,
            "peak_traced_memory_bytes": peak_memory_bytes,
            "tracemalloc_enabled": TRACEMALLOC_ENABLED,
            "read_only": True,
            "operator_called": False,
            "blend_saved": False,
        }
    finally:
        bm.free()


def run():
    try:
        evidence = main()
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(_clean(evidence), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print("PERF-P01 profile passed: %s" % RESULT_PATH)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    run()
