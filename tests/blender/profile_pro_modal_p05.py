"""PERF-P05 wrapper for dedicated completion/cancel interval profiles."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_modal.py"
SPEC = importlib.util.spec_from_file_location("pro_modal_p05_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load dedicated modal harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)


def _yield_every():
    value = BASE._arg_value("--yield-every", "64")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 64


YIELD_EVERY = _yield_every()
BASE.PACKET_ID = "PERF-P05-MODAL-Y%s" % YIELD_EVERY
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / ("pro_05_modal_y%s.json" % YIELD_EVERY)),
    )
).resolve()
_ORIGINAL_NEW_MODAL_OPERATOR = BASE._new_modal_operator


def _new_modal_operator_with_yield_profile(stack_tools, evidence):
    evidence["cooperative_yield_every"] = YIELD_EVERY
    return _ORIGINAL_NEW_MODAL_OPERATOR(stack_tools, evidence)


BASE._new_modal_operator = _new_modal_operator_with_yield_profile


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P05 modal failed: %s" % exc)
        raise SystemExit(1)
