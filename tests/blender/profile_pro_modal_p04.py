"""PERF-P04 wrapper for the dedicated completion/cancel modal harness."""

from pathlib import Path
import importlib.util
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_modal.py"
SPEC = importlib.util.spec_from_file_location("pro_modal_p04_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load dedicated modal harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)
BASE.PACKET_ID = "PERF-P04-MODAL"
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_04_modal_1024.json"),
    )
).resolve()

_ORIGINAL_NEW_MODAL_OPERATOR = BASE._new_modal_operator


def _new_modal_operator_with_timer_yield(stack_tools, evidence):
    operator, session = _ORIGINAL_NEW_MODAL_OPERATOR(stack_tools, evidence)
    original_modal = operator.modal

    def modal_with_timer_yield(*args, **kwargs):
        result = original_modal(*args, **kwargs)
        time.sleep(0.005)
        return result

    operator.modal = modal_with_timer_yield
    return operator, session


BASE._new_modal_operator = _new_modal_operator_with_timer_yield


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P04 modal failed: %s" % exc)
        raise SystemExit(1)
