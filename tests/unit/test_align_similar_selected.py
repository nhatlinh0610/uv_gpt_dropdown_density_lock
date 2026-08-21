"""Pure tests for the selected-only fixed-representative grouping policy."""

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_stack_tools_for_pure_helpers():
    """Load stack_tools with tiny Blender stubs for its pure helper tests."""

    module_name = "uv_gpt.stack_tools"
    if module_name in sys.modules:
        return sys.modules[module_name]

    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Operator=type("Operator", (), {}))
    bmesh = types.ModuleType("bmesh")
    mathutils = types.ModuleType("mathutils")

    class Vector(tuple):
        def __new__(cls, value):
            return tuple.__new__(cls, value)

    mathutils.Vector = Vector
    package = types.ModuleType("uv_gpt")
    package.__path__ = [str(PROJECT_ROOT / "uv_gpt")]

    sys.modules.setdefault("bpy", bpy)
    sys.modules.setdefault("bmesh", bmesh)
    sys.modules.setdefault("mathutils", mathutils)
    sys.modules.setdefault("uv_gpt", package)

    for name in ("island_tools", "uv_utils"):
        sys.modules.setdefault(
            f"uv_gpt.{name}", types.ModuleType(f"uv_gpt.{name}")
        )

    for name in ("match_scheduler", "similarity_matcher"):
        full_name = f"uv_gpt.{name}"
        if full_name in sys.modules:
            continue
        path = PROJECT_ROOT / "uv_gpt" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)

    path = PROJECT_ROOT / "uv_gpt" / "stack_tools.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


STACK_TOOLS = _load_stack_tools_for_pure_helpers()


@dataclass(frozen=True)
class Item:
    key: str
    family: str
    selected: bool = True


@dataclass(frozen=True)
class FakeResult:
    accepted: bool
    score: float
    transform: object


def _group(items, accepted_pairs, pair_filter_fn=None, tolerance=0.01):
    calls = []

    def prefetch(representative, future):
        calls.append(
            (representative.key, tuple(item.key for item in future))
        )
        result = {}
        for candidate in future:
            accepted = (representative.key, candidate.key) in accepted_pairs
            score = accepted_pairs.get((representative.key, candidate.key), 1.0)
            result[(representative.key, candidate.key)] = FakeResult(
                accepted=accepted,
                score=float(score),
                transform=(representative.key, candidate.key) if accepted else None,
            )
        return result

    ordered, groups = STACK_TOOLS._greedy_fixed_representative_groups(
        items,
        lambda item: item.key,
        prefetch,
        pair_filter_fn=pair_filter_fn,
        similarity_tolerance=tolerance,
    )
    return ordered, groups, calls


class AlignSimilarSelectedTests(unittest.TestCase):
    def test_repeated_families_exclude_unselected_sentinel(self):
        selected = [
            Item("b", "square"),
            Item("a", "square"),
            Item("c", "triangle"),
        ]
        unselected_sentinel = Item("sentinel", "square", selected=False)
        accepted = {
            ("a", "b"): 0.00001,
        }
        ordered, groups, _calls = _group(selected, accepted)

        self.assertEqual([item.key for item in ordered], ["a", "b", "c"])
        self.assertEqual(
            [group["representative"].key for group in groups], ["a", "c"]
        )
        self.assertEqual(
            [[item["island"].key for item in group["members"]] for group in groups],
            [["b"], []],
        )
        self.assertNotIn(unselected_sentinel.key, {item.key for item in ordered})

    def test_two_families_and_singleton_are_deterministic(self):
        items = [
            Item("d", "triangle"),
            Item("c", "triangle"),
            Item("e", "singleton"),
            Item("b", "square"),
            Item("a", "square"),
        ]
        accepted = {
            ("a", "b"): 0.00002,
            ("c", "d"): 0.00001,
        }
        first = _group(items, accepted)
        second = _group(list(reversed(items)), accepted)

        def signature(value):
            _ordered, groups, _calls = value
            return [
                (
                    group["representative"].key,
                    tuple(item["island"].key for item in group["members"]),
                )
                for group in groups
            ]

        self.assertEqual(signature(first), signature(second))
        self.assertEqual(
            signature(first),
            [("a", ("b",)), ("c", ("d",)), ("e", ())],
        )

    def test_members_use_fixed_representative_and_no_chain_drift(self):
        items = [Item("a", "family"), Item("b", "family"), Item("c", "family")]
        accepted = {
            ("a", "b"): 0.00002,
            ("a", "c"): 0.00001,
        }
        _ordered, groups, calls = _group(items, accepted)

        self.assertEqual([group["representative"].key for group in groups], ["a"])
        self.assertEqual(
            [member["result"].transform for member in groups[0]["members"]],
            [("a", "b"), ("a", "c")],
        )
        self.assertEqual(calls, [("a", ("b", "c"))])

    def test_transitivity_bridge_does_not_merge_through_member(self):
        items = [Item("a", "family"), Item("b", "family"), Item("c", "family")]
        accepted = {
            ("a", "b"): 0.00001,
            ("b", "c"): 0.00001,
        }
        _ordered, groups, _calls = _group(items, accepted)

        self.assertEqual(
            [
                (
                    group["representative"].key,
                    tuple(item["island"].key for item in group["members"]),
                )
                for group in groups
            ],
            [("a", ("b",)), ("c", ())],
        )

    def test_no_compatible_pair_has_zero_aligned_members(self):
        items = [Item("a", "x"), Item("b", "y"), Item("c", "z")]
        _ordered, groups, _calls = _group(items, {})

        self.assertEqual(sum(len(group["members"]) for group in groups), 0)
        self.assertEqual(sum(len(group["members"]) >= 1 for group in groups), 0)

    def test_selection_flags_are_not_mutated_by_grouping(self):
        items = [Item("a", "x"), Item("b", "x")]
        before = [(item.key, item.selected) for item in items]
        _group(items, {("a", "b"): 0.0})
        after = [(item.key, item.selected) for item in items]
        self.assertEqual(before, after)

    def test_ui_tolerance_accepts_full_fit_score_without_hidden_quality_gate(self):
        items = [Item("a", "family"), Item("b", "family")]
        _ordered, groups, _calls = _group(items, {("a", "b"): 0.001})
        self.assertEqual(
            [(group["representative"].key, [item["island"].key for item in group["members"]]) for group in groups],
            [("a", ["b"])],
        )

    def test_ui_tolerance_rejects_score_above_tolerance(self):
        items = [Item("a", "family"), Item("b", "family")]
        _ordered, groups, _calls = _group(
            items,
            {("a", "b"): 0.011},
            tolerance=0.01,
        )
        self.assertEqual(
            [(group["representative"].key, [item["island"].key for item in group["members"]]) for group in groups],
            [("a", []), ("b", [])],
        )

    def test_bucket_key_excludes_position(self):
        signature_type = types.SimpleNamespace
        first = signature_type(
            component_count=1,
            closed_component_count=1,
            open_component_count=0,
            ambiguous_component_count=0,
            degenerate_segment_count=0,
            cycle_count=1,
            raw_boundary_signature=(4, 4, (("closed", 4, 4, 1),)),
            center=(0.0, 0.0),
        )
        second = signature_type(**{**vars(first), "center": (10.0, -4.0)})
        self.assertEqual(
            STACK_TOOLS._cheap_group_bucket_key(first),
            STACK_TOOLS._cheap_group_bucket_key(second),
        )

    def test_invariant_bin_distance_gt_one_does_not_false_negative_grouping(self):
        items = [Item("a", "family"), Item("b", "family")]
        signatures = {
            "a": types.SimpleNamespace(invariant_signature=(1.0049, 2.0049)),
            "b": types.SimpleNamespace(invariant_signature=(1.0151, 2.0151)),
        }

        reference_bin = STACK_TOOLS._cheap_group_invariant_bin(signatures["a"])
        candidate_bin = STACK_TOOLS._cheap_group_invariant_bin(signatures["b"])
        self.assertGreater(
            max(
                abs(left - right)
                for left, right in zip(reference_bin, candidate_bin)
            ),
            1,
        )

        _ordered, groups, _calls = _group(
            items,
            {("a", "b"): 0.001},
            tolerance=0.01,
        )
        self.assertEqual(
            [(group["representative"].key, [item["island"].key for item in group["members"]]) for group in groups],
            [("a", ["b"])],
        )


if __name__ == "__main__":
    unittest.main()
