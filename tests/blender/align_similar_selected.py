"""AS-02 Blender 5.0 selected-only Align Similar harness.

The harness opens the supplied fixture in memory, verifies the exact saved
case before invoking the registered ``uv_gpt.align_to_selected`` compatibility
operator, and never saves the blend.  Each run restores the original UV
coordinates and selection flags before dispatch, which is the non-compounding
equivalent of reloading the fixture in a fresh Blender process.

The result is intentionally evidence-oriented: it records the selected-only
coverage, deterministic groups and representatives, matcher/scheduler
counters, post-fit quality, state preservation, and warmup/measured timings.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
import time


sys.dont_write_bytecode = True

import bmesh
import bpy
from mathutils import Vector


PACKET_ID = "AS-02"
SCHEMA_VERSION = "as-02-align-similar-selected-v2"
DEFAULT_FIXTURE = Path(r"C:\Users\linhp\Downloads\cc.blend")
EXPECTED_FIXTURE_SHA = (
    "76A72E7D0BB97E87D1EE5FABFFB9A57F6B175F9926AA98018AC3FD445D9BDD52"
)
TARGET_OBJECT_NAME = "Bottom.003"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_RAW_ISLANDS = 556
EXPECTED_SELECTED_ISLANDS = 556
EXPECTED_SELECTED_FACES = 24899
WARMUP_RUNS = 1
MEASURED_RUNS = 3
SELECTION_EPSILON = 1.0e-12


class HarnessError(RuntimeError):
    """Raised when fixture or AS-02 correctness evidence is invalid."""


class FixtureBlocker(HarnessError):
    """Raised when the exact fixture preflight facts do not match the packet."""


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


def _optional_bool_arg(name):
    value = _arg_value(name, "")
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise HarnessError("Invalid boolean argument %s=%s" % (name, value))


def apply_toggle_overrides(context):
    settings = context.scene.uv_gpt_settings
    overrides = {}
    for argument, attribute in (
        ("--match-scale", "stack_match_scale"),
        ("--allow-flipping", "stack_allow_flipping"),
    ):
        value = _optional_bool_arg(argument)
        if value is not None:
            setattr(settings, attribute, value)
            overrides[attribute] = value
    return overrides


SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_PROJECT_ROOT = SCRIPT_PATH.parents[2]
PROJECT_ROOT = Path(_arg_value("--project-root", str(DEFAULT_PROJECT_ROOT))).resolve()
FIXTURE_PATH = Path(_arg_value("--fixture", str(DEFAULT_FIXTURE))).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
PACKAGE_ZIP_PATH = Path(_arg_value("--package-zip", "")).resolve() if _arg_value("--package-zip", "") else None
_RESULT_ARGUMENT = _arg_value("--result", "")
RESULT_PATH = (
    Path(_RESULT_ARGUMENT).resolve()
    if _RESULT_ARGUMENT
    else PROJECT_ROOT
    / "benchmarks"
    / (
        "as_02_package_smoke.json"
        if PACKAGE_ZIP_PATH is not None
        else "as_02_align_similar_selected.json"
    )
)
PROFILE_RESULT_PATH = PROJECT_ROOT / "benchmarks" / "as_02_bucket_profile.json"
GATE_PROFILE_RESULT_PATH = PROJECT_ROOT / "benchmarks" / "as_02_gate_profile.json"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def clean_json(value):
    """Convert Blender and immutable diagnostic values to JSON-safe values."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Vector):
        return [clean_json(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return clean_json(value.to_dict())
    if hasattr(value, "__dict__"):
        return clean_json(vars(value))
    return str(value)


def setup_bmesh(bm):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()


def face_key(island_or_face):
    if hasattr(island_or_face, "index") and hasattr(island_or_face, "loops"):
        loops = island_or_face.loops
    else:
        loops = island_or_face
    return tuple(sorted({int(loop.face.index) for loop in loops}))


def loop_key(face, local_index):
    return int(face.index), int(local_index)


def island_loop_keys(island):
    keys = []
    for loop in island:
        for local_index, candidate in enumerate(loop.face.loops):
            if candidate is loop:
                keys.append(loop_key(loop.face, local_index))
                break
    return tuple(keys)


def snapshot_uv(bm, uv_layer):
    setup_bmesh(bm)
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            uv = loop[uv_layer].uv
            result[loop_key(face, local_index)] = (float(uv.x), float(uv.y))
    return result


def snapshot_selection(bm, uv_layer):
    setup_bmesh(bm)
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            result[loop_key(face, local_index)] = (
                bool(getattr(luv, "select", False)),
                bool(getattr(luv, "select_edge", False)),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
            )
    return result


def restore_state(obj, uv_utils, uv_snapshot, selection_snapshot):
    bm = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("UV layer disappeared while restoring baseline state")
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            value = uv_snapshot.get(loop_key(face, local_index))
            if value is not None:
                loop[uv_layer].uv = Vector(value)
    uv_utils.restore_uv_selection(bm, uv_layer, selection_snapshot)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("UV layer disappeared after baseline restore")
    return bm, uv_layer


def active_face_key(bm):
    try:
        active = bm.faces.active
    except Exception:
        active = None
    if active is None:
        return None
    return list(face_key(active))


def active_state(obj, bm, uv_layer):
    active_object = bpy.context.view_layer.objects.active
    context_object = getattr(bpy.context, "object", None)
    active_uv = getattr(obj.data.uv_layers, "active", None)
    tool_settings = getattr(bpy.context.scene, "tool_settings", None)
    return {
        "active_object": getattr(active_object, "name", None),
        "context_object": getattr(context_object, "name", None),
        "edit_object": getattr(getattr(bpy.context, "edit_object", None), "name", None),
        "mode": str(getattr(obj, "mode", None)),
        "active_face_key": active_face_key(bm),
        "active_uv_layer": getattr(active_uv, "name", None),
        "bmesh_uv_layer": getattr(uv_layer, "name", None),
        "use_uv_select_sync": bool(
            getattr(tool_settings, "use_uv_select_sync", False)
        ),
    }


def islands_by_key(islands):
    return {face_key(island): island for island in islands}


def max_uv_delta(before, after, keys):
    values = []
    for key in keys:
        lhs = before.get(key)
        rhs = after.get(key)
        if lhs is None or rhs is None:
            raise HarnessError("UV snapshot key disappeared: %s" % (key,))
        values.append(max(abs(lhs[0] - rhs[0]), abs(lhs[1] - rhs[1])))
    return max(values, default=0.0)


def island_delta(before, after, island):
    return max_uv_delta(before, after, island_loop_keys(island))


def settings_snapshot(context):
    settings = context.scene.uv_gpt_settings
    return {
        "stack_match_scale": bool(getattr(settings, "stack_match_scale", False)),
        "stack_allow_flipping": bool(getattr(settings, "stack_allow_flipping", False)),
        "stack_similarity_tolerance": float(
            getattr(settings, "stack_similarity_tolerance", 0.0)
        ),
        "active_uv_map_setting": str(getattr(settings, "active_uv_map", "")),
        "duplicate_before_operations": bool(
            getattr(settings, "duplicate_before_operations", False)
        ),
    }


def _scheduler_summary(value):
    if value is None:
        return None
    decision = getattr(value, "decision", None)
    diagnostics = getattr(value, "diagnostics", None)
    results = tuple(getattr(value, "results", ()) or ())
    statuses = {}
    for result in results:
        status = str(getattr(result, "status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "backend": str(getattr(decision, "backend", "")),
        "requested_backend": str(getattr(decision, "requested_backend", "")),
        "batch_size": int(getattr(decision, "batch_size", len(results))),
        "full_fit_count": getattr(decision, "full_fit_count", None),
        "worker_count": int(getattr(decision, "worker_count", 0)),
        "statuses": statuses,
        "diagnostics": clean_json(diagnostics),
    }


def _group_signature(evidence):
    return [
        {
            "representative_key": list(item["representative_key"]),
            "member_keys": [list(key) for key in item["member_keys"]],
            "size": int(item["size"]),
        }
        for item in evidence.get("groups", [])
    ]


def _quality_scores(
    stack_tools,
    similarity_matcher,
    obj,
    bm,
    uv_layer,
    evidence,
    settings,
):
    islands = islands_by_key(
        importlib.import_module("uv_gpt.island_tools").get_uv_islands(
            bm, uv_layer, selected_only=False
        )
    )
    cache = similarity_matcher.DescriptorCache(
        similarity_matcher.MatcherDiagnostics()
    )
    numeric_cache = {}
    identity = ("AS-02-quality", id(bm), len(islands))
    result = []
    tolerance = float(settings["stack_similarity_tolerance"])
    for record in evidence.get("apply_records", []):
        representative_key = tuple(record["representative_key"])
        member_key = tuple(record["member_key"])
        reference = stack_tools._descriptor_for_island(
            islands[representative_key],
            uv_layer,
            cache,
            identity,
            numeric_cache=numeric_cache,
        )
        candidate = stack_tools._descriptor_for_island(
            islands[member_key],
            uv_layer,
            cache,
            identity,
            numeric_cache=numeric_cache,
        )
        match = similarity_matcher.match_descriptors(
            reference,
            candidate,
            match_scale=bool(settings["stack_match_scale"]),
            allow_flipping=bool(settings["stack_allow_flipping"]),
            tolerance=tolerance,
            use_numpy=similarity_matcher.numpy_available(),
            diagnostics=similarity_matcher.MatcherDiagnostics(),
            allow_tolerant_topology=(
                tolerance > similarity_matcher.TOPOLOGY_PENALTY
            ),
            count_candidate=False,
        )
        result.append(
            {
                "representative_key": list(representative_key),
                "member_key": list(member_key),
                "accepted": bool(match.accepted),
                "normalized_rms": float(match.score)
                if math.isfinite(float(match.score))
                else None,
                "reason": str(match.reason),
            }
        )
    return result


def _validate_coverage(evidence, selected_keys):
    selected_set = set(selected_keys)
    seen = []
    group_sizes = []
    for group in evidence.get("groups", []):
        representative = tuple(group["representative_key"])
        members = [tuple(key) for key in group.get("member_keys", [])]
        if representative in members:
            raise HarnessError("A similarity group contains its representative as a member")
        if len(members) != len(set(members)):
            raise HarnessError("A similarity group assigns one member more than once")
        group_sizes.append(1 + len(members))
        seen.append(representative)
        seen.extend(members)
    if len(seen) != len(set(seen)):
        raise HarnessError("A selected island was assigned to more than one group")
    if set(seen) != selected_set:
        raise HarnessError(
            "Similarity groups do not cover exactly the selected islands: "
            f"missing={sorted(selected_set - set(seen))[:3]} "
            f"extra={sorted(set(seen) - selected_set)[:3]}"
        )
    expected_group_count = sum(size >= 2 for size in group_sizes)
    expected_aligned_count = sum(max(size - 1, 0) for size in group_sizes)
    if int(evidence.get("group_count", -1)) != expected_group_count:
        raise HarnessError("group_count does not match the evidence groups")
    if int(evidence.get("aligned_count", -1)) != expected_aligned_count:
        raise HarnessError("aligned_count does not match group member counts")
    return group_sizes


def run_one(
    obj,
    uv_utils,
    island_tools,
    stack_tools,
    similarity_matcher,
    baseline_uv,
    baseline_selection,
    baseline_keys,
    run_kind,
    run_index,
):
    bm, uv_layer = restore_state(
        obj, uv_utils, baseline_uv, baseline_selection
    )
    all_before = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected_before = [
        island
        for island in all_before
        if stack_tools._island_is_selected(island, uv_layer)
    ]
    selected_keys = tuple(sorted(face_key(island) for island in selected_before))
    if selected_keys != baseline_keys:
        raise HarnessError("Baseline restore changed selected island membership")
    before_uv = snapshot_uv(bm, uv_layer)
    before_selection = snapshot_selection(bm, uv_layer)
    before_state = active_state(obj, bm, uv_layer)
    settings = settings_snapshot(bpy.context)
    print(
        "AS-02 %s %d: dispatch selected-only operator" % (run_kind, run_index),
        flush=True,
    )
    started = time.perf_counter()
    evidence = stack_tools.run_match_03({"bpy_context": bpy.context})
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(
        "AS-02 %s %d: operator %.3f ms, aligned=%s groups=%s full_fits=%s" % (
            run_kind,
            run_index,
            elapsed_ms,
            evidence.get("aligned_count", 0),
            evidence.get("group_count", 0),
            getattr(evidence.get("diagnostics"), "full_fits", None),
        ),
        flush=True,
    )

    bm_after = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm_after)
    uv_layer_after = bm_after.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer_after is None:
        raise HarnessError("UV layer disappeared after Align Similar")
    all_after = island_tools.get_uv_islands(
        bm_after, uv_layer_after, selected_only=False
    )
    selected_after = [
        island
        for island in all_after
        if stack_tools._island_is_selected(island, uv_layer_after)
    ]
    after_uv = snapshot_uv(bm_after, uv_layer_after)
    after_selection = snapshot_selection(bm_after, uv_layer_after)
    after_state = active_state(obj, bm_after, uv_layer_after)
    before_by_key = islands_by_key(all_before)
    after_by_key = islands_by_key(all_after)
    selected_set = set(selected_keys)
    unselected_keys = tuple(
        sorted(key for key in before_by_key if key not in selected_set)
    )

    group_sizes = _validate_coverage(evidence, selected_keys)
    apply_records = [
        {
            "member_key": list(item["member_key"]),
            "representative_key": list(item["representative_key"]),
            "score": float(item["score"]),
        }
        for item in evidence.get("apply_records", [])
    ]
    member_keys = [tuple(item["member_key"]) for item in apply_records]
    if len(member_keys) != len(set(member_keys)):
        raise HarnessError("An applied selected island appears more than once")
    if set(member_keys) - selected_set:
        raise HarnessError("Apply evidence contains a non-selected island")
    representative_keys = [
        tuple(item["representative_key"])
        for item in evidence.get("groups", [])
    ]
    representative_deltas = {
        str(key): island_delta(before_uv, after_uv, after_by_key[key])
        for key in representative_keys
    }
    unselected_deltas = {
        str(key): island_delta(before_uv, after_uv, after_by_key[key])
        for key in unselected_keys
    }
    changed_island_keys = [
        key
        for key in sorted(after_by_key)
        if island_delta(before_uv, after_uv, after_by_key[key]) > SELECTION_EPSILON
    ]
    changed_islands = {
        str(key): island_delta(before_uv, after_uv, after_by_key[key])
        for key in changed_island_keys
    }
    applied_set = set(member_keys)
    changed_nonmembers = [
        list(key) for key in changed_island_keys if key not in applied_set
    ]
    quality = _quality_scores(
        stack_tools,
        similarity_matcher,
        obj,
        bm_after,
        uv_layer_after,
        evidence,
        settings,
    )
    print(
        "AS-02 %s %d: post-fit quality checked for %d members" % (
            run_kind,
            run_index,
            len(quality),
        ),
        flush=True,
    )
    quality_failures = [
        item
        for item in quality
        if not item["accepted"]
        or item["normalized_rms"] is None
        or item["normalized_rms"]
        > float(settings["stack_similarity_tolerance"])
    ]
    diagnostics = evidence.get("diagnostics")
    return {
        "run_kind": run_kind,
        "run_index": run_index,
        "elapsed_ms": elapsed_ms,
        "operator_result": evidence.get("operator_result", []),
        "operator_error": evidence.get("operator_error"),
        "report": "Aligned %d selected island(s) across %d similarity group(s)."
        % (int(evidence.get("aligned_count", 0)), int(evidence.get("group_count", 0))),
        "settings": settings,
        "case": {
            "raw_islands": len(all_before),
            "selected_islands": len(selected_before),
            "selected_faces": len(
                {loop.face.index for island in selected_before for loop in island}
            ),
            "unselected_islands": len(unselected_keys),
            "selected_keys_sha256": hashlib.sha256(
                repr(selected_keys).encode("utf-8")
            ).hexdigest(),
        },
        "groups": _group_signature(evidence),
        "group_sizes": group_sizes,
        "group_count": int(evidence.get("group_count", 0)),
        "aligned_count": int(evidence.get("aligned_count", 0)),
        "representative_keys": evidence.get("representative_keys", []),
        "apply_records": apply_records,
        "scheduler": _scheduler_summary(evidence.get("scheduler_result")),
        "diagnostics": clean_json(diagnostics),
        "correctness": {
            "selection_snapshot_unchanged": before_selection == after_selection,
            "unselected_max_delta": max(unselected_deltas.values(), default=0.0),
            "unselected_deltas": unselected_deltas,
            "representatives_max_delta": max(
                representative_deltas.values(), default=0.0
            ),
            "representative_deltas": representative_deltas,
            "changed_islands": changed_islands,
            "changed_nonmembers": changed_nonmembers,
            "quality": quality,
            "quality_failures": quality_failures,
            "active_state_unchanged": before_state == after_state,
            "before_state": before_state,
            "after_state": after_state,
            "no_duplicate_assignment": len(member_keys) == len(set(member_keys)),
        },
    }


def percentile(values, fraction):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    ratio = position - lower
    return values[lower] + (values[upper] - values[lower]) * ratio


def timing_summary(runs):
    values = [item["elapsed_ms"] for item in runs]
    return {
        "count": len(values),
        "min_ms": min(values) if values else None,
        "median_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "runs_ms": values,
    }


def verify_preflight(obj, island_tools, stack_tools):
    if obj is None or obj.type != "MESH":
        raise FixtureBlocker("Expected mesh object is missing: %s" % TARGET_OBJECT_NAME)
    active_object = bpy.context.view_layer.objects.active
    if getattr(active_object, "name", None) != TARGET_OBJECT_NAME:
        raise FixtureBlocker(
            "Active object mismatch: %r (expected %s)"
            % (getattr(active_object, "name", None), TARGET_OBJECT_NAME)
        )
    if obj.mode != "EDIT" or getattr(bpy.context, "edit_object", None) is not obj:
        raise FixtureBlocker("Expected active object is not in Edit Mode")
    active_uv = getattr(obj.data.uv_layers, "active", None)
    if getattr(active_uv, "name", None) != TARGET_UV_NAME:
        raise FixtureBlocker(
            "Active UV map mismatch: %r (expected %s)"
            % (getattr(active_uv, "name", None), TARGET_UV_NAME)
        )
    tool_settings = getattr(bpy.context.scene, "tool_settings", None)
    if getattr(tool_settings, "use_uv_select_sync", None) is not True:
        raise FixtureBlocker("Expected UV Sync selection mode to be enabled")

    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected = [
        island
        for island in all_islands
        if stack_tools._island_is_selected(island, uv_layer)
    ]
    selected_faces = {
        loop.face.index for island in selected for loop in island
    }
    if len(all_islands) != EXPECTED_RAW_ISLANDS:
        raise FixtureBlocker(
            "Raw visible island count mismatch: %d (expected %d)"
            % (len(all_islands), EXPECTED_RAW_ISLANDS)
        )
    if len(selected) != EXPECTED_SELECTED_ISLANDS:
        raise FixtureBlocker(
            "Selected island count mismatch: %d (expected %d)"
            % (len(selected), EXPECTED_SELECTED_ISLANDS)
        )
    if len(selected_faces) != EXPECTED_SELECTED_FACES:
        raise FixtureBlocker(
            "Selected face count mismatch: %d (expected %d)"
            % (len(selected_faces), EXPECTED_SELECTED_FACES)
        )
    return bm, uv_layer, all_islands, selected


def bucket_profile(obj, bm, uv_layer, all_islands, selected, stack_tools, similarity_matcher):
    """Profile the one-pass cheap partition without running or mutating the operator."""

    similarity_matcher.reset_diagnostics()
    descriptor_cache = similarity_matcher.DescriptorCache()
    numeric_cache = {}
    identity = stack_tools._snapshot_identity(obj, bm, uv_layer, all_islands)
    buckets = {}
    for island in sorted(selected, key=face_key):
        key = face_key(island)
        signature = stack_tools._cheap_signature_for_island(
            island,
            uv_layer,
            descriptor_cache,
            identity,
            numeric_cache,
        )
        bucket = stack_tools._cheap_group_bucket_key(signature)
        buckets.setdefault(bucket, []).append(key)
    sizes = sorted((len(keys) for keys in buckets.values()), reverse=True)
    result = {
        "status": "passed",
        "packet": PACKET_ID,
        "schema": "as-02-bucket-profile-v1",
        "fixture_sha256": sha256_file(FIXTURE_PATH),
        "raw_islands": len(all_islands),
        "selected_islands": len(selected),
        "bucket_count": len(buckets),
        "bucket_size_summary": {
            "max": max(sizes, default=0),
            "min": min(sizes, default=0),
            "top_20": sizes[:20],
        },
        "buckets": [
            {
                "key": clean_json(bucket),
                "size": len(keys),
                "face_keys": [list(key) for key in sorted(keys)],
            }
            for bucket, keys in sorted(buckets.items(), key=lambda item: item[0])
        ],
        "diagnostics": clean_json(similarity_matcher.get_diagnostics()),
    }
    PROFILE_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_RESULT_PATH.write_text(
        json.dumps(clean_json(result), indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    print("AS-02 bucket profile: %s" % PROFILE_RESULT_PATH, flush=True)
    print(json.dumps(clean_json(result["bucket_size_summary"]), sort_keys=True), flush=True)
    return result


def gate_profile(obj, bm, uv_layer, all_islands, selected, stack_tools, similarity_matcher):
    """Count cheap gate outcomes without constructing descriptors or fitting."""

    similarity_matcher.reset_diagnostics()
    descriptor_cache = similarity_matcher.DescriptorCache()
    numeric_cache = {}
    identity = stack_tools._snapshot_identity(obj, bm, uv_layer, all_islands)
    signatures = {}
    for island in sorted(selected, key=face_key):
        key = face_key(island)
        signatures[key] = stack_tools._cheap_signature_for_island(
            island,
            uv_layer,
            descriptor_cache,
            identity,
            numeric_cache,
        )
    buckets = {}
    for key, signature in signatures.items():
        buckets.setdefault(stack_tools._cheap_group_bucket_key(signature), []).append(key)
    settings = settings_snapshot(bpy.context)
    invariant_widths = (0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.35)
    counts = {
        "bucket_pair_count": 0,
        "neighbor_query_pair_count": 0,
        "neighbor_excluded_pair_count": 0,
        "boundary_pass": 0,
        "topology_pass": 0,
        "strict_topology_pass": 0,
        "tolerant_topology_pass": 0,
        "would_reach_full_fit": 0,
        "rejected_boundary": 0,
        "rejected_topology": 0,
        "rejected_tolerance_topology": 0,
    }
    for width in invariant_widths:
        label = str(width).replace(".", "_")
        counts["same_invariant_bin_pairs_%s" % label] = 0
        counts["same_invariant_bin_gate_pass_%s" % label] = 0

    def invariant_bin(signature, width):
        return tuple(int(math.floor(value / width)) for value in signature.invariant_signature)

    for bucket_islands in buckets.values():
        ordered = sorted(bucket_islands)
        for index, reference_key in enumerate(ordered):
            for candidate_key in ordered[index + 1 :]:
                counts["bucket_pair_count"] += 1
                if not stack_tools._cheap_group_invariant_neighbors(
                    signatures[reference_key], signatures[candidate_key]
                ):
                    counts["neighbor_excluded_pair_count"] += 1
                    continue
                counts["neighbor_query_pair_count"] += 1
                for width in invariant_widths:
                    label = str(width).replace(".", "_")
                    if invariant_bin(signatures[reference_key], width) == invariant_bin(
                        signatures[candidate_key], width
                    ):
                        counts["same_invariant_bin_pairs_%s" % label] += 1
                boundary = similarity_matcher.cheap_boundary_gate(
                    signatures[reference_key], signatures[candidate_key]
                )
                if not boundary.passed:
                    counts["rejected_boundary"] += 1
                    continue
                counts["boundary_pass"] += 1
                for width in invariant_widths:
                    label = str(width).replace(".", "_")
                    if invariant_bin(signatures[reference_key], width) == invariant_bin(
                        signatures[candidate_key], width
                    ):
                        counts["same_invariant_bin_gate_pass_%s" % label] += 1
                topology = similarity_matcher.cheap_topology_gate(
                    signatures[reference_key], signatures[candidate_key]
                )
                if not topology.passed:
                    counts["rejected_topology"] += 1
                    continue
                counts["topology_pass"] += 1
                if topology.strict:
                    counts["strict_topology_pass"] += 1
                else:
                    counts["tolerant_topology_pass"] += 1
                if (
                    topology.strict
                    or float(settings["stack_similarity_tolerance"])
                    > similarity_matcher.TOPOLOGY_PENALTY
                ):
                    counts["would_reach_full_fit"] += 1
                else:
                    counts["rejected_tolerance_topology"] += 1
    result = {
        "status": "passed",
        "packet": PACKET_ID,
        "schema": "as-02-gate-profile-v1",
        "fixture_sha256": sha256_file(FIXTURE_PATH),
        "raw_islands": len(all_islands),
        "selected_islands": len(selected),
        "settings": settings,
        "counts": counts,
        "diagnostics": clean_json(similarity_matcher.get_diagnostics()),
    }
    GATE_PROFILE_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    GATE_PROFILE_RESULT_PATH.write_text(
        json.dumps(clean_json(result), indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    print("AS-02 gate profile: %s" % GATE_PROFILE_RESULT_PATH, flush=True)
    print(json.dumps(clean_json(result["counts"]), sort_keys=True), flush=True)
    return result


def run_harness():
    if not FIXTURE_PATH.is_file():
        raise FixtureBlocker("Fixture missing: %s" % FIXTURE_PATH)
    in_process_sha = sha256_file(FIXTURE_PATH)
    if FIXTURE_SHA_BEFORE_EXTERNAL and in_process_sha != FIXTURE_SHA_BEFORE_EXTERNAL:
        raise FixtureBlocker(
            "Fixture SHA changed between runner and Blender: external=%s in_process=%s"
            % (FIXTURE_SHA_BEFORE_EXTERNAL, in_process_sha)
        )
    if in_process_sha != EXPECTED_FIXTURE_SHA:
        raise FixtureBlocker(
            "Unexpected fixture SHA before run: %s (expected %s)"
            % (in_process_sha, EXPECTED_FIXTURE_SHA)
        )
    if Path(bpy.data.filepath).resolve() != FIXTURE_PATH:
        raise FixtureBlocker(
            "Blender opened a different file: %s (expected %s)"
            % (bpy.data.filepath, FIXTURE_PATH)
        )
    if PACKAGE_ZIP_PATH is not None:
        if not PACKAGE_ZIP_PATH.is_file():
            raise HarnessError("Package ZIP missing: %s" % PACKAGE_ZIP_PATH)
        if str(PACKAGE_ZIP_PATH) not in sys.path:
            sys.path.insert(0, str(PACKAGE_ZIP_PATH))

    addon = importlib.import_module("uv_gpt")
    island_tools = importlib.import_module("uv_gpt.island_tools")
    stack_tools = importlib.import_module("uv_gpt.stack_tools")
    similarity_matcher = importlib.import_module("uv_gpt.similarity_matcher")
    uv_utils = importlib.import_module("uv_gpt.uv_utils")
    registered = False
    try:
        addon.register()
        registered = True
        toggle_overrides = apply_toggle_overrides(bpy.context)
        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        bm, uv_layer, all_islands, selected = verify_preflight(
            obj, island_tools, stack_tools
        )
        if _arg_value("--profile-only", "").lower() in {"1", "true", "yes"}:
            bucket_profile(
                obj,
                bm,
                uv_layer,
                all_islands,
                selected,
                stack_tools,
                similarity_matcher,
            )
            return
        if _arg_value("--gate-profile", "").lower() in {"1", "true", "yes"}:
            gate_profile(
                obj,
                bm,
                uv_layer,
                all_islands,
                selected,
                stack_tools,
                similarity_matcher,
            )
            return
        baseline_uv = snapshot_uv(bm, uv_layer)
        baseline_selection = snapshot_selection(bm, uv_layer)
        baseline_keys = tuple(sorted(face_key(island) for island in selected))
        runs = []
        for index in range(WARMUP_RUNS):
            runs.append(
                run_one(
                    obj,
                    uv_utils,
                    island_tools,
                    stack_tools,
                    similarity_matcher,
                    baseline_uv,
                    baseline_selection,
                    baseline_keys,
                    "warmup",
                    index + 1,
                )
            )
        measured = []
        for index in range(MEASURED_RUNS):
            item = run_one(
                obj,
                uv_utils,
                island_tools,
                stack_tools,
                similarity_matcher,
                baseline_uv,
                baseline_selection,
                baseline_keys,
                "measured",
                index + 1,
            )
            runs.append(item)
            measured.append(item)

        if len(measured) != MEASURED_RUNS:
            raise HarnessError("Measured run count mismatch")
        signatures = [
            (item["groups"], item["representative_keys"], item["group_count"], item["aligned_count"])
            for item in measured
        ]
        if any(signature != signatures[0] for signature in signatures[1:]):
            raise HarnessError("Measured group/representative signature is not repeatable")
        for index, item in enumerate(measured, 1):
            if item["operator_result"] != ["FINISHED"] or item["operator_error"]:
                raise HarnessError("Measured run %d did not finish" % index)
            if item["aligned_count"] <= 0 or item["group_count"] <= 0:
                raise HarnessError(
                    "Measured run %d produced no selected similarity stack" % index
                )
            correctness = item["correctness"]
            if not correctness["selection_snapshot_unchanged"]:
                raise HarnessError("Measured run %d changed selection flags" % index)
            if correctness["unselected_max_delta"] > SELECTION_EPSILON:
                raise HarnessError("Measured run %d changed unselected UVs" % index)
            if correctness["representatives_max_delta"] > SELECTION_EPSILON:
                raise HarnessError("Measured run %d changed a representative" % index)
            if correctness["changed_nonmembers"]:
                raise HarnessError(
                    "Measured run %d changed a non-member island: %s"
                    % (index, correctness["changed_nonmembers"])
                )
            if not correctness["active_state_unchanged"]:
                raise HarnessError("Measured run %d changed active Blender state" % index)
            if correctness["quality_failures"]:
                raise HarnessError(
                    "Measured run %d failed post-fit RMS: %s"
                    % (index, correctness["quality_failures"][:3])
                )
            diagnostics = item.get("diagnostics") or {}
            full_fits = int(diagnostics.get("full_fits", 0))
            candidates_seen = int(diagnostics.get("candidates_seen", 0))
            if full_fits <= 0 or candidates_seen <= 0:
                raise HarnessError(
                    "Measured run %d lacks representative/full-fit diagnostics" % index
                )

        fixture_sha_after = sha256_file(FIXTURE_PATH)
        if fixture_sha_after != in_process_sha:
            raise HarnessError(
                "Fixture SHA changed in Blender process: before=%s after=%s"
                % (in_process_sha, fixture_sha_after)
            )
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "schema": SCHEMA_VERSION,
            "fixture": {
                "path": str(FIXTURE_PATH),
                "sha256_before": in_process_sha,
                "sha256_after": fixture_sha_after,
                "raw_islands": len(all_islands),
                "selected_islands": len(selected),
                "selected_faces": EXPECTED_SELECTED_FACES,
                "selection_semantics": "current _island_is_selected helper; UV Sync on",
                "uv_sync_off": "known limit: current helper is preserved; not proven by this packet",
            },
            "case": {
                "active_object": TARGET_OBJECT_NAME,
                "active_uv_map": TARGET_UV_NAME,
                "mode": "EDIT",
                "metric_note": "raw visible islands; not the UVPackmaster region metric",
            },
            "package": {
                "mode": "zip-import" if PACKAGE_ZIP_PATH is not None else "source-import",
                "zip_path": str(PACKAGE_ZIP_PATH) if PACKAGE_ZIP_PATH is not None else None,
                "zip_sha256": sha256_file(PACKAGE_ZIP_PATH)
                if PACKAGE_ZIP_PATH is not None
                else None,
                "loaded_from": str(getattr(addon, "__file__", "")),
            },
            "toggle_overrides": toggle_overrides,
            "warmup_runs": WARMUP_RUNS,
            "measured_runs": MEASURED_RUNS,
            "timing": timing_summary(measured),
            "runs": runs,
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        print("AS-02 result: %s" % RESULT_PATH)
        print(json.dumps(clean_json(result["timing"]), sort_keys=True))
        return result
    finally:
        if registered:
            addon.unregister()


def main():
    try:
        run_harness()
    except Exception as exc:
        failure = {
            "status": "blocked" if isinstance(exc, FixtureBlocker) else "failed",
            "packet": PACKET_ID,
            "schema": SCHEMA_VERSION,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
        try:
            RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULT_PATH.write_text(
                json.dumps(clean_json(failure), indent=2, sort_keys=True),
                encoding="utf-8",
                newline="\n",
            )
        except Exception:
            pass
        print(json.dumps(clean_json(failure), sort_keys=True))
        import traceback

        traceback.print_exc()
        return 2 if isinstance(exc, FixtureBlocker) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
