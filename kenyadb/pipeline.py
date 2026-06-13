"""Pipeline orchestration.

Reads the registry, walks every source in every (selected) layer, dispatches
to the right handler, and flushes provenance to both a JSON manifest and the
DuckDB provenance table.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import handlers
from .handlers import Ctx
from .utils.provenance import Provenance

OPEN_ACCESS = {"open_api", "open_download"}


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run(
    config_path: Path,
    base_dir: Path,
    *,
    layers: list[str] | None = None,
    only_open: bool = False,
    dry_run: bool = False,
) -> Provenance:
    cfg = load_config(config_path)
    raw_dir = base_dir / "data" / "raw"
    external_dir = base_dir / "data" / "external"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    manifest = base_dir / "logs" / f"manifest_{run_id}.json"
    prov = Provenance(run_id, manifest)

    selected = layers or list(cfg["layers"].keys())
    print(f"[run] run_id={run_id} layers={selected} only_open={only_open} dry_run={dry_run}")

    for layer in selected:
        sources = cfg["layers"].get(layer, {})
        for source_key, meta in sources.items():
            access = meta.get("access", "")
            handler_name = meta.get("handler", "manual")
            if only_open and access not in OPEN_ACCESS:
                prov.record(layer=layer, source_key=source_key, meta=meta,
                            status="skipped", message=f"only_open: access={access}")
                print(f"  - {layer}/{source_key}: skipped ({access})")
                continue
            fn = handlers.HANDLERS.get(handler_name, handlers.manual)
            ctx = Ctx(layer=layer, source_key=source_key, raw_dir=raw_dir,
                      external_dir=external_dir, prov=prov, dry_run=dry_run)
            print(f"  - {layer}/{source_key}: {handler_name} ({access})")
            try:
                fn(meta, ctx)
            except Exception as exc:  # noqa: BLE001 - never let one source kill the run
                prov.record(layer=layer, source_key=source_key, meta=meta,
                            status="failed", message=f"handler error: {exc}")

    prov.to_manifest()
    print(f"[run] manifest -> {manifest}")
    return prov
