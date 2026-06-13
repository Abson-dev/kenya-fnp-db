"""Download and IO helpers: retrying HTTP, checksums, idempotent fetch.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

USER_AGENT = "kenya-fnp-db/1.0 (research pipeline; Aboubacar HEMA)"
DEFAULT_TIMEOUT = 120


def sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_download(
    url: str,
    dest: Path,
    *,
    overwrite: bool = False,
    retries: int = 4,
    backoff: float = 3.0,
    headers: dict | None = None,
    params: dict | None = None,
) -> Path:
    """Download url to dest with retries. Idempotent unless overwrite=True.

    Returns the destination path. Raises on permanent failure so the caller
    can record a failed provenance entry.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        return dest

    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(
                url, stream=True, timeout=DEFAULT_TIMEOUT, headers=hdrs, params=params
            ) as resp:
                resp.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
                tmp.replace(dest)
            return dest
        except Exception as exc:  # noqa: BLE001 - we retry then re-raise
            last_err = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"download failed after {retries} attempts: {url}") from last_err


def get_json(url: str, *, params: dict | None = None, retries: int = 4,
             backoff: float = 3.0, headers: dict | None = None) -> dict | list:
    """GET a JSON endpoint with retries."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT, headers=hdrs)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"json request failed: {url}") from last_err
