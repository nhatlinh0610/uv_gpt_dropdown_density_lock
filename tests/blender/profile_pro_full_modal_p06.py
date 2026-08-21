"""PERF-P06 one-shot full-577 modal validation.

The base harness drives the registered Pro modal method with TIMER events,
which is the same session state machine used by the user invocation.  This
wrapper changes only packet/result labels and deliberately adds no sleep or
worker simulation.
"""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_full_modal.py"
SPEC = importlib.util.spec_from_file_location("pro_full_modal_p06_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load full modal harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)

BASE.PACKET_ID = "PERF-P06-FULL-MODAL"
BASE.COOPERATIVE_YIELD_EVERY = None
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_06_full_modal.json"),
    )
).resolve()


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P06 full-modal failed: %s" % exc)
        raise SystemExit(1)
