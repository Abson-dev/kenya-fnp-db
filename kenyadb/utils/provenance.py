"""Provenance tracking.

Records, for every acquisition attempt: source key, publisher, mirror,
access type, license, URL, local path, file checksum, file size, status,
extraction timestamp (UTC) and any message. The bundle document explicitly
asks to store original publisher, mirror, extraction date and version, so
this table is the reproducibility backbone of the whole database.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PROVENANCE_DDL = """
CREATE TABLE IF NOT EXISTS provenance (
    run_id        VARCHAR,
    layer         VARCHAR,
    source_key    VARCHAR,
    title         VARCHAR,
    publisher     VARCHAR,
    mirror        VARCHAR,
    access        VARCHAR,
    license       VARCHAR,
    url           VARCHAR,
    local_path    VARCHAR,
    sha256        VARCHAR,
    bytes         BIGINT,
    status        VARCHAR,      -- ok | manual | skipped | failed
    message       VARCHAR,
    extracted_at  TIMESTAMP
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Provenance:
    """Accumulates records, then flushes to DuckDB and a JSON manifest."""

    def __init__(self, run_id: str, manifest_path: Path):
        self.run_id = run_id
        self.manifest_path = Path(manifest_path)
        self.records: list[dict] = []

    def record(
        self,
        *,
        layer: str,
        source_key: str,
        meta: dict,
        local_path: str | None = None,
        sha256: str | None = None,
        nbytes: int | None = None,
        status: str = "ok",
        message: str = "",
    ) -> None:
        self.records.append(
            {
                "run_id": self.run_id,
                "layer": layer,
                "source_key": source_key,
                "title": meta.get("title", source_key),
                "publisher": meta.get("publisher", ""),
                "mirror": meta.get("mirror", ""),
                "access": meta.get("access", ""),
                "license": meta.get("license", ""),
                "url": meta.get("url") or meta.get("base_url") or meta.get("catalog", ""),
                "local_path": local_path or "",
                "sha256": sha256 or "",
                "bytes": int(nbytes) if nbytes is not None else None,
                "status": status,
                "message": message,
                "extracted_at": utc_now().isoformat(),
            }
        )

    def to_manifest(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"run_id": self.run_id, "records": self.records},
                fh,
                indent=2,
                ensure_ascii=False,
            )

    def to_duckdb(self, con) -> None:
        con.execute(PROVENANCE_DDL)
        if not self.records:
            return
        import pandas as pd

        df = pd.DataFrame(self.records)
        df["extracted_at"] = pd.to_datetime(df["extracted_at"])
        con.register("_prov_tmp", df)
        con.execute("INSERT INTO provenance SELECT * FROM _prov_tmp")
        con.unregister("_prov_tmp")
