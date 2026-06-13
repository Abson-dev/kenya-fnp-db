"""Assemble the DuckDB database from acquired files and the crosswalk.

Loads:
  - core.crosswalk_admin   (master join key)
  - core.source_registry   (flattened registry for discoverability)
  - provenance             (written by the Provenance object)
  - any normalised CSV/Parquet placed under data/processed/<layer>/ by
    transform scripts, registered into the matching schema as a view.

This keeps the database thin and reproducible: heavy rasters and microdata
stay on disk as files, while the database holds the tabular indicators, the
crosswalk, and the full provenance ledger that ties them together.

Author: Aboubacar HEMA
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .utils.db import connect, has_spatial


def _load_crosswalk(con, processed_dir: Path) -> None:
    xwalk = processed_dir / "crosswalk_admin.csv"
    if xwalk.exists():
        con.execute(
            "CREATE OR REPLACE TABLE core.crosswalk_admin AS "
            "SELECT * FROM read_csv_auto(?, header=true)",
            [str(xwalk)],
        )
        n = con.execute("SELECT count(*) FROM core.crosswalk_admin").fetchone()[0]
        print(f"[db] core.crosswalk_admin loaded ({n} rows)")


def _load_registry(con, config_path: Path) -> None:
    cfg = yaml.safe_load(open(config_path, encoding="utf-8"))
    rows = []
    for layer, sources in cfg["layers"].items():
        for key, meta in sources.items():
            rows.append({
                "layer": layer,
                "source_key": key,
                "title": meta.get("title", key),
                "publisher": meta.get("publisher", ""),
                "mirror": meta.get("mirror", ""),
                "access": meta.get("access", ""),
                "license": meta.get("license", ""),
                "role": meta.get("role", ""),
                "url": meta.get("url") or meta.get("base_url") or meta.get("catalog", ""),
            })
    import pandas as pd
    df = pd.DataFrame(rows)
    con.register("_reg_tmp", df)
    con.execute("CREATE OR REPLACE TABLE core.source_registry AS SELECT * FROM _reg_tmp")
    con.unregister("_reg_tmp")
    print(f"[db] core.source_registry loaded ({len(rows)} sources)")


def _register_processed(con, processed_dir: Path) -> None:
    """Register normalised outputs from transforms into their layer schema."""
    for layer in ("geography", "soil", "food", "health", "policy"):
        ldir = processed_dir / layer
        if not ldir.exists():
            continue
        for f in sorted(ldir.glob("*.csv")) + sorted(ldir.glob("*.parquet")):
            tbl = f.stem.lower().replace("-", "_")
            reader = "read_parquet" if f.suffix == ".parquet" else "read_csv_auto"
            con.execute(
                f"CREATE OR REPLACE TABLE {layer}.{tbl} AS "
                f"SELECT * FROM {reader}('{f}'{', header=true' if reader=='read_csv_auto' else ''})"
            )
            print(f"[db] {layer}.{tbl} registered from {f.name}")


def build(base_dir: Path, config_path: Path, db_path: Path, prov=None) -> Path:
    processed = base_dir / "data" / "processed"
    con = connect(db_path)
    print(f"[db] spatial extension: {'available' if has_spatial(con) else 'absent'}")

    _load_crosswalk(con, processed)
    _load_registry(con, config_path)
    _register_processed(con, processed)

    if prov is not None:
        prov.to_duckdb(con)
        n = con.execute("SELECT count(*) FROM provenance").fetchone()[0]
        ok = con.execute("SELECT count(*) FROM provenance WHERE status='ok'").fetchone()[0]
        man = con.execute("SELECT count(*) FROM provenance WHERE status='manual'").fetchone()[0]
        fail = con.execute("SELECT count(*) FROM provenance WHERE status='failed'").fetchone()[0]
        print(f"[db] provenance: {n} rows (ok={ok}, manual={man}, failed={fail})")

    con.close()
    print(f"[db] database -> {db_path}")
    return db_path
