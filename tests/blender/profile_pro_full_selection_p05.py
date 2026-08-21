"""PERF-P05 wrapper for full-577 background interval profiles."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_full_selection.py"
SPEC = importlib.util.spec_from_file_location("pro_full_selection_p05_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load full-selection harness")
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
BASE.PACKET_ID = "PERF-P05-FULL-Y%s" % YIELD_EVERY
BASE.COOPERATIVE_YIELD_EVERY = YIELD_EVERY
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / ("pro_05_full_y%s.json" % YIELD_EVERY)),
    )
).resolve()


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P05 full-selection failed: %s" % exc)
        raise SystemExit(1)
