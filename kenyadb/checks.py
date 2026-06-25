"""Shared helpers for the per-layer "is everything okay" diagnostics.

Each `check_<layer>.py` at the repository root builds a `CheckResult` and prints
it; `check_all.py` runs them together and rolls the verdicts up into one
summary. Everything here is read-only and needs no rebuild: the checks inspect
the data folders and the built database, they never write or download.

Status vocabulary:
  PASS  the thing is present and looks right
  WARN  present but not as expected, or an optional layer that is absent
  FAIL  an essential thing is missing or broken
  INFO  a neutral statement of fact (for example, anaemia is absent by design)
  SKIP  could not be checked (for example, the database is not built yet)
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"
_MARK = {PASS: "ok  ", WARN: "WARN", FAIL: "FAIL", INFO: "--  ", SKIP: "skip"}


class CheckResult:
    """A small accumulator of per-check verdicts for one layer."""

    def __init__(self, layer: str):
        self.layer = layer
        self.items: list[tuple[str, str, str]] = []  # (status, label, detail)

    def add(self, status: str, label: str, detail: str = "") -> "CheckResult":
        self.items.append((status, label, detail))
        return self

    def ok(self, label, detail=""):
        return self.add(PASS, label, detail)

    def warn(self, label, detail=""):
        return self.add(WARN, label, detail)

    def fail(self, label, detail=""):
        return self.add(FAIL, label, detail)

    def info(self, label, detail=""):
        return self.add(INFO, label, detail)

    def skip(self, label, detail=""):
        return self.add(SKIP, label, detail)

    @property
    def overall(self) -> str:
        statuses = [s for s, _, _ in self.items]
        if FAIL in statuses:
            return FAIL
        if WARN in statuses:
            return WARN
        if PASS in statuses:
            return PASS
        # only INFO and/or SKIP items remain: nothing was actually verified
        return SKIP

    def counts(self) -> Counter:
        return Counter(s for s, _, _ in self.items)

    def print_report(self) -> None:
        print(f"\n=== {self.layer} ===")
        for status, label, detail in self.items:
            line = f"  [{_MARK[status]}] {label}"
            if detail:
                line += f": {detail}"
            print(line)
        c = self.counts()
        print(f"  -> {self.overall}  "
              f"({c.get(PASS, 0)} ok, {c.get(WARN, 0)} warn, "
              f"{c.get(FAIL, 0)} fail, {c.get(SKIP, 0)} skip)")

    def to_markdown(self) -> str:
        lines = [f"### {self.layer}", ""]
        for status, label, detail in self.items:
            line = f"- [{status}] {label}"
            if detail:
                line += f": {detail}"
            lines.append(line)
        c = self.counts()
        lines.append("")
        lines.append(f"Verdict: {self.overall} "
                     f"({c.get(PASS, 0)} ok, {c.get(WARN, 0)} warn, "
                     f"{c.get(FAIL, 0)} fail, {c.get(SKIP, 0)} skip)")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Database helpers (read-only)
# --------------------------------------------------------------------------- #
def connect(base: Path):
    """Open the built database read-only, or return None if it is absent or
    duckdb is not installed."""
    db = base / "data" / "db" / "kenya_fnp.duckdb"
    if not db.exists():
        return None
    try:
        import duckdb
    except ImportError:
        return None
    return duckdb.connect(str(db), read_only=True)


def has_table(con, schema: str, table: str) -> bool:
    q = ("select count(*) from information_schema.tables "
         "where table_schema = ? and table_name = ?")
    return con.execute(q, [schema, table]).fetchone()[0] > 0


def count(con, schema: str, table: str) -> int:
    return con.execute(f'select count(*) from "{schema}"."{table}"').fetchone()[0]


def columns(con, schema: str, table: str) -> list[str]:
    return [c[0] for c in
            con.execute(f'select * from "{schema}"."{table}" limit 0').description]


def nonnull(con, schema: str, table: str, col: str) -> int:
    return con.execute(
        f'select count(*) from "{schema}"."{table}" where "{col}" is not null'
    ).fetchone()[0]


def schema_tables(con, schema: str) -> list[str]:
    rows = con.execute(
        "select table_name from information_schema.tables "
        "where table_schema = ? order by table_name", [schema]).fetchall()
    return [r[0] for r in rows]


def table_check(res: CheckResult, con, schema: str, table: str,
                expect: int | None = 47, essential: bool = True) -> int | None:
    """Standard existence and row-count check. Returns the row count (or None)."""
    if con is None:
        res.skip(f"{schema}.{table}", "database not built")
        return None
    if not has_table(con, schema, table):
        (res.fail if essential else res.warn)(f"{schema}.{table}", "missing")
        return None
    n = count(con, schema, table)
    if expect is not None and n != expect:
        res.warn(f"{schema}.{table}", f"{n} rows (expected {expect})")
    else:
        res.ok(f"{schema}.{table}", f"{n} rows")
    return n


def coverage_check(res: CheckResult, con, schema: str, table: str, col: str,
                   expect: int = 47, essential: bool = True) -> None:
    """Check non-null coverage of one column against an expected county count."""
    if con is None or not has_table(con, schema, table):
        return
    if col not in columns(con, schema, table):
        (res.fail if essential else res.warn)(f"{schema}.{table}.{col}", "column absent")
        return
    n = nonnull(con, schema, table, col)
    if n >= expect:
        res.ok(f"{schema}.{table}.{col}", f"{n}/{expect} populated")
    elif n > 0:
        res.warn(f"{schema}.{table}.{col}", f"{n}/{expect} populated")
    else:
        (res.fail if essential else res.warn)(f"{schema}.{table}.{col}", "all null")


def provenance_summary(res: CheckResult, con, layer: str) -> None:
    if con is None or not has_table(con, "provenance", "provenance") \
            and not _has_provenance(con):
        return
    try:
        rows = con.execute(
            "select status, count(*) from provenance where layer = ? "
            "group by status order by status", [layer]).fetchall()
    except Exception:  # noqa: BLE001
        return
    if rows:
        detail = ", ".join(f"{st}={c}" for st, c in rows)
        res.info(f"provenance ({layer})", detail)


def _has_provenance(con) -> bool:
    try:
        con.execute("select 1 from provenance limit 1")
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def folder_stats(d: Path):
    if not d.exists():
        return [], 0
    files = [p for p in d.rglob("*") if p.is_file()]
    return files, sum(p.stat().st_size for p in files)
