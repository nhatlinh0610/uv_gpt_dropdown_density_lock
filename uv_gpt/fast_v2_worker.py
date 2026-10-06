"""Standalone worker for Fast V2.

Usage: python fast_v2_worker.py INPUT_PICKLE OUTPUT_PICKLE PROGRESS_JSON
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pickle
import sys
import time
import traceback

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import fast_v2_core  # noqa: E402


def _atomic_bytes(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    input_path = Path(argv[1])
    output_path = Path(argv[2])
    progress_path = Path(argv[3])
    started = time.perf_counter()
    last_progress_write = 0.0

    def progress(values: dict) -> None:
        nonlocal last_progress_write
        now = time.perf_counter()
        # Keep filesystem traffic bounded while still giving the UI useful updates.
        if values.get("percent") != 100.0 and now - last_progress_write < 0.04:
            return
        payload = dict(values)
        payload["elapsed"] = max(0.0, now - started)
        payload["pid"] = os.getpid()
        try:
            _atomic_json(progress_path, payload)
            last_progress_write = now
        except OSError:
            pass

    try:
        progress({"stage": "load", "percent": 1.0, "done": 0, "total": 1})
        with input_path.open("rb") as handle:
            snapshot = pickle.load(handle)
        result = fast_v2_core.solve_fast(snapshot, progress_cb=progress)
        payload = {"ok": True, "result": result}
        _atomic_bytes(output_path, pickle.dumps(payload, protocol=5))
        progress({
            "stage": "done",
            "percent": 100.0,
            "done": int(result.get("selected_count", 0)),
            "total": max(1, int(result.get("selected_count", 0))),
        })
        return 0
    except BaseException as exc:  # worker boundary: transfer bounded diagnostics
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "traceback": traceback.format_exc()[-6000:],
        }
        try:
            _atomic_bytes(output_path, pickle.dumps(payload, protocol=5))
            progress({"stage": "failed", "percent": 100.0, "done": 0, "total": 1})
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
