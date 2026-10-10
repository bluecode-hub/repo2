"""
reset.py - clear accumulated experiment data for a clean slate, without touching the
stable reference layer (dependency_map.json, baseline.json come from mapper.py / monitor.py
and are regenerated on their own schedule, not per-experiment).

Usage:
    python reset.py            # clear and confirm
    python reset.py --yes      # clear without the confirmation prompt (used by the
                                # dashboard's "reset" button, which has no terminal to prompt)
"""
import sys
from pathlib import Path

DATA = Path("data")
CLEARED_FILES = ["injection_comparison.jsonl", "runs.jsonl", "live_log.jsonl", "chaos_state.json"]


def reset():
    for name in CLEARED_FILES:
        (DATA / name).unlink(missing_ok=True)


if __name__ == "__main__":
    if "--yes" not in sys.argv:
        answer = input(f"Clear {', '.join(CLEARED_FILES)} and start fresh? [y/N]: ").strip().lower()
        if answer != "y":
            print("Not cleared.")
            sys.exit(0)
    reset()
    print("Cleared. Next day1_pipeline.py run starts from a clean baseline.")
