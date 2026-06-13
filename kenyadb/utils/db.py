"""DuckDB database helper: connection, spatial extension, schema.

The database is organised as five thematic schemas plus a shared crosswalk
and a provenance table. DuckDB is the default engine because it needs no
server, reads CSV / Parquet / GeoPackage natively, and the spatial
extension covers the geometry joins. Swap to PostGIS by pointing the
loaders at a libpq connection instead - the table layout is identical.

Author: Aboubacar HEMA
"""
from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMAS = ["geography", "soil", "food", "health", "policy", "core"]


def connect(db_path: Path, *, spatial: bool = True) -> duckdb.DuckDBPyConnection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    if spatial:
        try:
            con.execute("INSTALL spatial; LOAD spatial;")
        except Exception as exc:  # noqa: BLE001 - spatial is optional offline
            print(f"[db] spatial extension unavailable ({exc}); "
                  "geometry loaders will be skipped")
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    return con


def has_spatial(con: duckdb.DuckDBPyConnection) -> bool:
    try:
        con.execute("SELECT ST_Point(0,0);")
        return True
    except Exception:  # noqa: BLE001
        return False
