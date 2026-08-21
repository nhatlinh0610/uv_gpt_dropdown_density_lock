"""PERF-P05 wrapper for full-577 modal interval profiles."""

from pathlib import Path
import importlib.util
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_full_modal.py"
SPEC = importlib.util.spec_from_file_location("pro_full_modal_p05_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load full modal harness")
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
BASE.PACKET_ID = "PERF-P05-FULL-MODAL-Y%s" % YIELD_EVERY
BASE.COOPERATIVE_YIELD_EVERY = YIELD_EVERY
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / ("pro_05_full_modal_y%s.json" % YIELD_EVERY)),
    )
).resolve()
_ORIGINAL_NEW_MODAL_OPERATOR = BASE.MODAL._new_modal_operator


def _new_modal_operator_with_timer_yield(stack_tools, evidence):
    operator, session = _ORIGINAL_NEW_MODAL_OPERATOR(stack_tools, evidence)
    original_modal = operator.modal

    def modal_with_timer_yield(*args, **kwargs):
        result = original_modal(*args, **kwargs)
        time.sleep(0.005)
        return result

    operator.modal = modal_with_timer_yield
    return operator, session


BASE.MODAL._new_modal_operator = _new_modal_operator_with_timer_yield


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P05 full-modal failed: %s" % exc)
        raise SystemExit(1)
