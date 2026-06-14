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

import json
from pathlib import Path

import yaml

from .utils.db import connect, has_spatial
from .utils.provenance import PROVENANCE_DDL


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


_PROV_COLS = ["run_id", "layer", "source_key", "title", "publisher", "mirror",
              "access", "license", "url", "local_path", "sha256", "bytes",
              "status", "message", "extracted_at"]


def _merge_local_provenance(con, base_dir: Path) -> int:
    """Fold provenance sidecars written by local extractors (e.g. the
    action_plan module) into the ledger as authoritative, current rows.

    For each data/processed/<layer>/*_provenance.json, any prior rows for that
    source_key are removed and one fresh row is inserted, so a locally-provided
    source reads as acquired even on a --build-only run (which never touches the
    network and so never refreshes the main Provenance object). This is what
    flips action_plan from absent/manual to ok in the acquisition report.
    """
    con.execute(PROVENANCE_DDL)
    proc = base_dir / "data" / "processed"
    if not proc.exists():
        return 0
    import pandas as pd

    merged = 0
    for sc in sorted(proc.glob("*/*_provenance.json")):
        try:
            d = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        sk = d.get("source_key")
        if not sk:
            continue
        n_csv = len(d.get("csv_outputs") or [])
        anchors = d.get("verification_anchors", 0)
        verified = anchors - len(d.get("anchors_missing") or [])
        msg = d.get("message") or (
            f"local extract: {d.get('pages', '?')} pages, {n_csv} CSV tables, "
            f"anchors {verified}/{anchors} verified")
        row = {
            "run_id": "local",
            "layer": d.get("layer", "core"),
            "source_key": sk,
            "title": d.get("title", sk),
            "publisher": d.get("publisher", ""),
            "mirror": None,
            "access": d.get("access", "local"),
            "license": d.get("license"),
            "url": d.get("pdf_path") or d.get("url"),
            "local_path": d.get("pdf_path"),
            "sha256": d.get("sha256"),
            "bytes": d.get("bytes"),
            "status": "ok" if d.get("sha256") else "manual",
            "message": msg,
            "extracted_at": d.get("extracted_at"),
        }
        df = pd.DataFrame([row], columns=_PROV_COLS)
        df["extracted_at"] = pd.to_datetime(df["extracted_at"], utc=True, errors="coerce")
        con.execute("DELETE FROM provenance WHERE source_key = ?", [sk])
        con.register("_sc_tmp", df)
        con.execute("INSERT INTO provenance SELECT * FROM _sc_tmp")
        con.unregister("_sc_tmp")
        merged += 1
    return merged


def build(base_dir: Path, config_path: Path, db_path: Path, prov=None) -> Path:
    processed = base_dir / "data" / "processed"
    con = connect(db_path)
    print(f"[db] spatial extension: {'available' if has_spatial(con) else 'absent'}")

    _load_crosswalk(con, processed)
    _load_registry(con, config_path)
    _register_processed(con, processed)

    if prov is not None:
        prov.to_duckdb(con)

    # Fold in locally-provided sources (e.g. action_plan) so they read as
    # acquired regardless of run mode, then report the current ledger.
    merged = _merge_local_provenance(con, base_dir)
    if merged:
        print(f"[db] merged {merged} local provenance record(s)")
    if prov is not None or merged:
        n = con.execute("SELECT count(*) FROM provenance").fetchone()[0]
        ok = con.execute("SELECT count(*) FROM provenance WHERE status='ok'").fetchone()[0]
        man = con.execute("SELECT count(*) FROM provenance WHERE status='manual'").fetchone()[0]
        fail = con.execute("SELECT count(*) FROM provenance WHERE status='failed'").fetchone()[0]
        print(f"[db] provenance: {n} rows (ok={ok}, manual={man}, failed={fail})")

    con.close()
    print(f"[db] database -> {db_path}")
    return db_path
