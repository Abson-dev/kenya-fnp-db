#!/usr/bin/env python3
"""Validate the assembled Kenya FNP database.

Runs two complementary, read-only validations and writes one markdown report:

  1. the cross-layer consistency checks (crosswalk integrity, county-name join
     coverage, per-source sanity and provenance) from kenyadb.validate, and
  2. the per-layer health checks (geography, soil, food, body/health, policy)
     that confirm each dimension is present and analysis-ready, including the
     iSDA micronutrients, remote sensing, the KDHS 2022 and 2014 rounds, the
     soil index and the county policy panel.

Usage:
  python validate.py                  # checks data/db/kenya_fnp.duckdb
  python validate.py --db path/to.duckdb
  python validate.py --no-layer-checks   # only the consistency report

Read-only: it never modifies the database. The report is written to
data/processed/validation_report.md. The exit code is non-zero if any layer
reports a FAIL, so validate.py can gate an analysis run or a push.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kenyadb import validate
from kenyadb.checks import FAIL, PASS, SKIP, WARN

import check_geography
import check_soil
import check_food
import check_health
import check_policy

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "db" / "kenya_fnp.duckdb"
LAYERS = [check_geography, check_soil, check_food, check_health, check_policy]
_ORDER = {PASS: 0, SKIP: 1, WARN: 2, FAIL: 3}


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the Kenya FNP database")
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--no-layer-checks", action="store_true",
                    help="run only the cross-layer consistency report")
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db} (run run_all.py first)")

    # 1. cross-layer consistency report (writes validation_report.md)
    report_path = validate.run(args.db, BASE)

    if args.no_layer_checks:
        return 0

    # 2. per-layer health checks (cover the layers the consistency report predates).
    # These read the standard project data tree under the repository root.
    print("\n=== Per-layer health checks ===")
    results = [mod.run(BASE) for mod in LAYERS]
    for res in results:
        res.print_report()

    worst = PASS
    for res in results:
        if _ORDER[res.overall] > _ORDER[worst]:
            worst = res.overall

    # append the per-layer checks to the same report so the deliverable is complete
    try:
        with open(report_path, "a", encoding="utf-8") as fh:
            fh.write("\n\n# Per-layer health checks\n\n")
            for res in results:
                fh.write(res.to_markdown())
                fh.write("\n\n")
            fh.write(f"Overall layer verdict: {worst}\n")
        print(f"\nappended per-layer checks -> {report_path}")
    except OSError as exc:  # noqa: BLE001
        print(f"could not append the layer checks to the report: {exc}")

    print(f"\nOverall layer verdict: {worst}")
    return 1 if worst == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
