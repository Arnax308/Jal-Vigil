"""Run the agent once from the command line and print its trace.

    python scripts/smoke_run.py                     # live forecast
    python scripts/smoke_run.py 2023-12-03          # replay (Cyclone Michaung)
    python scripts/smoke_run.py 2023-12-03 "check Velachery for the next 48 hours"
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.runner import execute  # noqa: E402
from app.events import registry  # noqa: E402


def main() -> None:
    replay = sys.argv[1] if len(sys.argv) > 1 else None
    query = sys.argv[2] if len(sys.argv) > 2 else None
    run = registry.create({
        "mode": "replay" if replay else "live", "replay_date": replay, "horizon_hours": 72, "query": query,
    })
    started = time.time()
    execute(run)
    for e in run.events:
        print(f"[{e['type']:<11}] {e['step'] or '':<18} {e['message']}")
    print(f"\nstatus: {run.status} in {time.time() - started:.1f}s")

    result = run.result or {}
    if "cells" in result:
        print(f"cells {len(result['cells']['features'])}, facilities at risk {len(result['facilities_at_risk'])}, "
              f"routes {len(result['routes'])}")
        for s in result["sources"]:
            print(f"  source: {s['source']} (cached={s['from_cache']}, stale={s['stale']})")
        alert = result.get("alert")
        if alert:
            print(f"\nalert by {alert['generated_by']}: {alert['headline_en']}\n{alert['body_en']}")
            if alert.get("body_ta"):
                print(f"\n{alert['headline_ta']}\n{alert['body_ta']}")


if __name__ == "__main__":
    main()
