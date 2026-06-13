#!/usr/bin/env python3
"""Kenya Soil / Food / Nutrition / Policy database - top-level runner.

Examples
--------
  # See the full plan without touching the network:
  python run_all.py --dry-run

  # Acquire only the immediately-open sources, then build the database:
  python run_all.py --only-open

  # Run a single layer:
  python run_all.py --layer soil

  # Full run (open sources fetched; gated ones recorded as manual gates):
  python run_all.py

  # Rebuild the database from already-downloaded files only:
  python run_all.py --build-only

Author: Aboubacar HEMA
"""
from __future__ import annotations

import argparse
from pathlib import Path

from kenyadb import crosswalk, pipeline, transforms
from kenyadb import build_db as builder

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config" / "sources.yaml"
DB = BASE / "data" / "db" / "kenya_fnp.duckdb"


def main() -> None:
    ap = argparse.ArgumentParser(description="Kenya FNP database pipeline")
    ap.add_argument("--layer", action="append",
                    choices=["geography", "soil", "food", "health", "policy"],
                    help="run only this layer (repeatable)")
    ap.add_argument("--only-open", action="store_true",
                    help="fetch only open_api / open_download sources")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and record intentions, fetch nothing")
    ap.add_argument("--build-only", action="store_true",
                    help="skip acquisition; rebuild crosswalk + database from disk")
    ap.add_argument("--no-transform", action="store_true",
                    help="skip the normalisation transforms before the build")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args()

    prov = None
    if not args.build_only:
        prov = pipeline.run(
            args.config, BASE,
            layers=args.layer, only_open=args.only_open, dry_run=args.dry_run,
        )

    # Master crosswalk (county seed always; sub-county enrichment if COD-AB present)
    crosswalk.build(BASE / "data" / "raw", BASE / "data" / "processed")

    if not args.dry_run:
        if not args.no_transform:
            transforms.run_all(BASE)
        builder.build(BASE, args.config, args.db, prov=prov)
    else:
        print("[run] dry-run: database build skipped")


if __name__ == "__main__":
    main()
