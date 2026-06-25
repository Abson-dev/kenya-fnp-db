#!/usr/bin/env python3
"""Global check: run every per-layer diagnostic and roll the verdicts up.

Runs the geography, soil, food, body/health and policy checks, prints each
report, then a one-line summary per layer and an overall verdict. Read-only;
needs no rebuild.

Run from the project root:  python check_all.py
Exit code is 1 if any layer reports a FAIL, else 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

from kenyadb.checks import FAIL, PASS, SKIP, WARN, connect

import check_geography
import check_soil
import check_food
import check_health
import check_policy

BASE = Path(__file__).resolve().parent
LAYERS = [check_geography, check_soil, check_food, check_health, check_policy]
_MARK = {PASS: "ok  ", WARN: "WARN", FAIL: "FAIL", SKIP: "skip"}


def main() -> int:
    print("Kenya FNP - global health check")
    print("=" * 60)

    db = BASE / "data" / "db" / "kenya_fnp.duckdb"
    if not db.exists():
        print("\nNote: the database is not built yet, so table checks will be")
        print("skipped. Build it with `python run_all.py --build-only` first.")

    results = [mod.run(BASE) for mod in LAYERS]
    for res in results:
        res.print_report()

    # roll-up
    print("\n" + "=" * 60)
    print("Summary by layer")
    print("-" * 60)
    worst = PASS
    order = {PASS: 0, SKIP: 1, WARN: 2, FAIL: 3}
    for res in results:
        c = res.counts()
        print(f"  [{_MARK[res.overall]}] {res.layer:<34} "
              f"{c.get(PASS, 0)} ok / {c.get(WARN, 0)} warn / "
              f"{c.get(FAIL, 0)} fail / {c.get(SKIP, 0)} skip")
        if order[res.overall] > order[worst]:
            worst = res.overall

    print("-" * 60)
    verdict = {PASS: "all layers healthy",
               WARN: "healthy with warnings (see WARN items above)",
               FAIL: "one or more layers have a FAIL; see above",
               SKIP: "nothing could be checked (database not built)"}[worst]
    print(f"Overall: {_MARK[worst].strip().upper()} - {verdict}")
    return 1 if worst == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
