import ast
import hashlib
import json
import math
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = PROJECT_ROOT / "tests" / "blender" / "profile_pro_process_mc4.py"


def _load_canonical_helpers():
    tree = ast.parse(PROFILE_PATH.read_text(encoding="utf-8"))
    wanted = {
        "_canonical",
        "_digest",
        "_nonfinite_token",
        "_path_for_index",
        "_path_for_key",
        "_unique_nonfinite_paths",
    }
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {
        "hashlib": hashlib,
        "json": json,
        "math": math,
        "Path": Path,
        "NONFINITE_TOKEN_KEY": "__float__",
    }
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(PROFILE_PATH), "exec"), namespace)
    return namespace


class Mc4HarnessCanonicalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_canonical_helpers()

    def test_nested_nonfinite_values_are_tagged_and_reported_by_path(self):
        canonical = self.helpers["_canonical"]
        paths = []
        value = {
            "outer": {
                "finite": -0.0,
                "nan": float("nan"),
                "negative": (float("-inf"),),
                "positive": [float("inf")],
            }
        }

        result = canonical(value, nonfinite_paths=paths)

        self.assertEqual(result["outer"]["nan"], {"__float__": "nan"})
        self.assertEqual(
            result["outer"]["negative"][0], {"__float__": "neg_inf"}
        )
        self.assertEqual(
            result["outer"]["positive"][0], {"__float__": "pos_inf"}
        )
        self.assertEqual(result["outer"]["finite"], -0.0)
        self.assertEqual(math.copysign(1.0, result["outer"]["finite"]), -1.0)
        self.assertEqual(
            paths,
            [
                {"path": '$["outer"]["nan"]', "type": "float", "token": "nan"},
                {
                    "path": '$["outer"]["negative"][0]',
                    "type": "float",
                    "token": "neg_inf",
                },
                {
                    "path": '$["outer"]["positive"][0]',
                    "type": "float",
                    "token": "pos_inf",
                },
            ],
        )
        json.dumps(result, allow_nan=False, sort_keys=True)

    def test_digest_is_order_independent_and_lossless_for_float_tokens(self):
        digest = self.helpers["_digest"]
        first_paths = []
        second_paths = []
        first = {
            "b": [float("nan"), -0.0],
            "a": {"positive": float("inf"), "negative": float("-inf")},
        }
        second = {
            "a": {"negative": float("-inf"), "positive": float("inf")},
            "b": [float("nan"), -0.0],
        }

        first_digest = digest(first, nonfinite_paths=first_paths)
        second_digest = digest(second, nonfinite_paths=second_paths)

        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_paths, second_paths)
        self.assertEqual(len(first_paths), 3)
        self.assertNotEqual(
            digest({"value": -0.0}), digest({"value": 0.0})
        )

    def test_duplicate_evidence_paths_are_stably_deduplicated(self):
        unique = self.helpers["_unique_nonfinite_paths"]
        values = [
            {"path": '$["x"]', "type": "float", "token": "nan"},
            {"path": '$["x"]', "type": "float", "token": "nan"},
            {"path": '$["a"]', "type": "float", "token": "pos_inf"},
        ]

        self.assertEqual(
            unique(values),
            [
                {"path": '$["a"]', "type": "float", "token": "pos_inf"},
                {"path": '$["x"]', "type": "float", "token": "nan"},
            ],
        )

    def test_profile_retains_lossless_session_report_on_failure_path(self):
        source = PROFILE_PATH.read_text(encoding="utf-8")
        self.assertIn('"session_report": canonical_result', source)
        self.assertIn('"nonfinite_paths": _unique_nonfinite_paths', source)
        self.assertIn("allow_nan=False", source)
        self.assertIn('"traceback":', source)


if __name__ == "__main__":
    unittest.main()
