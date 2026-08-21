"""PERF-P04 wrapper for the existing full modal-path harness."""

from pathlib import Path
import importlib.util
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_full_modal.py"
SPEC = importlib.util.spec_from_file_location("pro_full_modal_p04_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load full modal harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)
BASE.PACKET_ID = "PERF-P04-FULL-MODAL"
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_04_full_modal_1024.json"),
    )
).resolve()

_ORIGINAL_NEW_MODAL_OPERATOR = BASE.MODAL._new_modal_operator


def _new_modal_operator_with_timer_yield(stack_tools, evidence):
    operator, session = _ORIGINAL_NEW_MODAL_OPERATOR(stack_tools, evidence)
    original_modal = operator.modal

    def modal_with_timer_yield(*args, **kwargs):
        result = original_modal(*args, **kwargs)
        # The background harness calls the registered modal method directly;
        # this short yield models Blender's TIMER interval and lets the single
        # pure-Python worker receive GIL time between polls.
        time.sleep(0.005)
        return result

    operator.modal = modal_with_timer_yield
    return operator, session


BASE.MODAL._new_modal_operator = _new_modal_operator_with_timer_yield


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P04 full-modal failed: %s" % exc)
        raise SystemExit(1)
