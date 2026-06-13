#!/usr/bin/env python3
"""Run consistency checks on the assembled Kenya FNP database.

Usage:
  python validate.py            # checks data/db/kenya_fnp.duckdb
  python validate.py --db path/to.duckdb

Read-only: it never modifies the database. Writes a report to
data/processed/validation_report.md.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import argparse
from pathlib import Path

from kenyadb import validate

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "db" / "kenya_fnp.duckdb"


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the Kenya FNP database")
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db} (run run_all.py first)")
    validate.run(args.db, BASE)


if __name__ == "__main__":
    main()
