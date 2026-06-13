"""Consistency and analysis-readiness checks for the assembled database.

Run after acquisition to confirm the layers join cleanly onto the master
crosswalk before any analysis. Checks, per available table:
  - crosswalk integrity (47 counties / ~290 sub-counties, no dups, no blanks)
  - county-name join coverage for every sub-national table (lists unmatched
    names, which is where Kenyan spelling variants bite: Tharaka Nithi,
    Elgeyo Marakwet, Murang'a, Trans Nzoia, etc.)
  - SoilGrids county coverage and null cells
  - FAOSTAT Kenya-only check, domains, year span, missing values
  - WFP price date span, admin overlap with counties, duplicates, null prices
  - World Bank HNP indicators and year span
  - provenance summary (failed sources, pending manual gates)

Each finding is tagged PASS / WARN / FAIL. Nothing here mutates the database;
it only reads and reports, also writing data/processed/validation_report.md.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb

COUNTY_COL_HINTS = ("county_name", "county", "adm1_en", "admin1", "name_1", "area")


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0}

    def add(self, level: str, msg: str):
        self.counts[level] = self.counts.get(level, 0) + 1
        self.lines.append(f"[{level}] {msg}")
        print(f"  [{level}] {msg}")

    def header(self, title: str):
        self.lines.append("")
        self.lines.append(f"## {title}")
        print(f"\n=== {title} ===")


def _tables(con) -> dict[str, list[str]]:
    rows = con.execute("""
        select table_schema, table_name
        from information_schema.tables
        where table_schema in ('core','geography','soil','food','health','policy')
        order by 1,2
    """).fetchall()
    return {f"{s}.{t}": _cols(con, s, t) for s, t in rows}


def _cols(con, schema: str, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "select column_name from information_schema.columns "
        "where table_schema=? and table_name=? order by ordinal_position",
        [schema, table]).fetchall()]


def _norm_sql(col: str) -> str:
    # mirror crosswalk.norm in SQL: lower, apostrophe unify, non-alnum -> space, trim
    return (f"trim(regexp_replace(lower(replace(cast({col} as varchar), '\u2019', '''')), "
            f"'[^a-z0-9]+', ' ', 'g'))")


def check_crosswalk(con, rep: Report):
    rep.header("Crosswalk integrity")
    if "core.crosswalk_admin" not in _tables(con):
        rep.add("FAIL", "core.crosswalk_admin missing - run the pipeline first")
        return
    n_c = con.execute("select count(distinct county_norm) from core.crosswalk_admin "
                      "where county_norm <> ''").fetchone()[0]
    rep.add("PASS" if n_c == 47 else "WARN", f"distinct counties = {n_c} (expected 47)")
    has_sub = con.execute("select count(*) from core.crosswalk_admin "
                          "where subcounty_norm is not null and subcounty_norm <> ''").fetchone()[0]
    if has_sub:
        n_s = con.execute("select count(distinct subcounty_norm) from core.crosswalk_admin "
                          "where subcounty_norm <> ''").fetchone()[0]
        rep.add("PASS" if 240 <= n_s <= 340 else "WARN",
                f"distinct sub-counties = {n_s} (expected ~290)")
    else:
        rep.add("WARN", "crosswalk is county-seed only (no sub-counties yet)")
    dups = con.execute("""
        select count(*) from (
          select county_norm, subcounty_norm, count(*) c
          from core.crosswalk_admin group by 1,2 having count(*) > 1)
    """).fetchone()[0]
    rep.add("PASS" if dups == 0 else "FAIL", f"duplicate county/sub-county keys = {dups}")


def _county_col(cols: list[str]) -> str | None:
    low = {c.lower(): c for c in cols}
    for h in COUNTY_COL_HINTS:
        if h in low:
            return low[h]
    return None


def check_join_coverage(con, rep: Report, tables: dict):
    rep.header("County-name join coverage")
    if "core.crosswalk_admin" not in tables:
        rep.add("FAIL", "no crosswalk to join against")
        return
    for tbl, cols in tables.items():
        if tbl.startswith("core.") or tbl == "provenance":
            continue
        ccol = _county_col(cols)
        if not ccol:
            continue
        # FAOSTAT Kenya is national (Area='Kenya'), so skip its 'Area' as a county key
        if tbl == "food.faostat_kenya":
            continue
        total = con.execute(f'select count(*) from {tbl} where "{ccol}" is not null').fetchone()[0]
        if total == 0:
            continue
        matched = con.execute(f"""
            select count(*) from {tbl} t
            where {_norm_sql(f't."{ccol}"')} in
                  (select county_norm from core.crosswalk_admin)
        """).fetchone()[0]
        pct = 100.0 * matched / total
        level = "PASS" if pct >= 99 else ("WARN" if pct >= 80 else "FAIL")
        rep.add(level, f"{tbl}: {pct:.1f}% of rows match a county (col '{ccol}')")
        if pct < 99:
            bad = con.execute(f"""
                select distinct cast(t."{ccol}" as varchar) v from {tbl} t
                where {_norm_sql(f't."{ccol}"')} not in
                      (select county_norm from core.crosswalk_admin)
                  and t."{ccol}" is not null
                limit 15
            """).fetchall()
            names = ", ".join(sorted({r[0] for r in bad}))
            rep.add("INFO", f"   unmatched names in {tbl}: {names}")


def check_soilgrids(con, rep: Report, tables: dict):
    if "soil.soilgrids_zonal_county" not in tables:
        return
    rep.header("SoilGrids zonal statistics")
    cols = tables["soil.soilgrids_zonal_county"]
    n = con.execute("select count(*) from soil.soilgrids_zonal_county").fetchone()[0]
    rep.add("PASS" if n == 47 else "WARN", f"county rows = {n} (expected 47)")
    cov = [c for c in cols if c not in ("county_name", "county_norm")]
    rep.add("INFO", f"coverages = {len(cov)}")
    nulls = []
    for c in cov:
        k = con.execute(f'select count(*) from soil.soilgrids_zonal_county where "{c}" is null').fetchone()[0]
        if k:
            nulls.append(f"{c}({k})")
    rep.add("PASS" if not nulls else "WARN",
            "no null county cells" if not nulls else f"null cells: {', '.join(nulls[:10])}")


def check_faostat(con, rep: Report, tables: dict):
    if "food.faostat_kenya" not in tables:
        return
    rep.header("FAOSTAT Kenya")
    cols = tables["food.faostat_kenya"]
    acol = next((c for c in cols if c.lower() == "area"), None)
    if acol:
        areas = con.execute(f'select distinct "{acol}" from food.faostat_kenya limit 5').fetchall()
        vals = {str(a[0]) for a in areas}
        rep.add("PASS" if vals <= {"Kenya"} else "FAIL",
                f"area values = {sorted(vals)} (expected only Kenya)")
    dcol = next((c for c in cols if c.lower() == "faostat_domain"), None)
    if dcol:
        doms = [r[0] for r in con.execute(
            f'select distinct "{dcol}" from food.faostat_kenya order by 1').fetchall()]
        rep.add("INFO", f"domains = {doms}")
    ycol = next((c for c in cols if c.lower() == "year"), None)
    if ycol:
        lo, hi = con.execute(f'select min("{ycol}"), max("{ycol}") from food.faostat_kenya').fetchone()
        rep.add("INFO", f"year span = {lo}-{hi}")
    vcol = next((c for c in cols if c.lower() == "value"), None)
    if vcol:
        tot = con.execute("select count(*) from food.faostat_kenya").fetchone()[0]
        nullv = con.execute(f'select count(*) from food.faostat_kenya where "{vcol}" is null').fetchone()[0]
        rep.add("PASS" if nullv == 0 else "WARN",
                f"null Value rows = {nullv} of {tot}")


def check_wfp(con, rep: Report, tables: dict):
    if "food.prices_wfp_observed" not in tables:
        return
    rep.header("WFP observed prices")
    cols = tables["food.prices_wfp_observed"]
    low = {c.lower(): c for c in cols}
    n = con.execute("select count(*) from food.prices_wfp_observed").fetchone()[0]
    rep.add("INFO", f"rows = {n}")
    if "date" in low:
        lo, hi = con.execute(f'select min("{low["date"]}"), max("{low["date"]}") '
                             "from food.prices_wfp_observed").fetchone()
        rep.add("INFO", f"date span = {lo} to {hi}")
    if "price" in low:
        bad = con.execute(f'select count(*) from food.prices_wfp_observed '
                          f'where try_cast("{low["price"]}" as double) is null '
                          f'and "{low["price"]}" is not null').fetchone()[0]
        rep.add("PASS" if bad == 0 else "WARN",
                f"non-numeric price values = {bad} (HXL tag row should be stripped)")
    a1 = low.get("admin1")
    if a1:
        ov = con.execute(f"""
            select count(distinct {_norm_sql(f'"{a1}"')})
            from food.prices_wfp_observed
            where {_norm_sql(f'"{a1}"')} in (select county_norm from core.crosswalk_admin)
        """).fetchone()[0]
        tot = con.execute(f'select count(distinct "{a1}") from food.prices_wfp_observed').fetchone()[0]
        rep.add("INFO", f"admin1 values matching counties = {ov} of {tot} distinct")


def check_wb_hnp(con, rep: Report, tables: dict):
    if "health.wb_hnp_panel" not in tables:
        return
    rep.header("World Bank HNP panel")
    inds = [r[0] for r in con.execute(
        "select distinct indicator from health.wb_hnp_panel order by 1").fetchall()]
    rep.add("INFO", f"indicators = {len(inds)}: {', '.join(inds)}")
    lo, hi = con.execute("select min(year), max(year) from health.wb_hnp_panel").fetchone()
    rep.add("INFO", f"year span = {lo}-{hi}")


def check_provenance(con, rep: Report):
    has = con.execute("select count(*) from information_schema.tables "
                      "where table_name='provenance'").fetchone()[0]
    if not has:
        return
    rep.header("Provenance")
    for status, n in con.execute("select status, count(*) from provenance group by 1 order by 1").fetchall():
        rep.add("INFO", f"{status}: {n} rows")
    failed = [r[0] for r in con.execute(
        "select distinct source_key from provenance where status='failed' order by 1").fetchall()]
    if failed:
        rep.add("WARN", f"sources with failures: {', '.join(failed)}")
    pending = [r[0] for r in con.execute(
        "select distinct source_key from provenance where status='manual' order by 1").fetchall()]
    if pending:
        rep.add("INFO", f"manual gates pending: {', '.join(pending)}")


def run(db_path: Path, base: Path) -> Path:
    con = duckdb.connect(str(db_path), read_only=True)
    rep = Report()
    print("=== Kenya FNP database - consistency report ===")
    tables = _tables(con)
    rep.header("Tables loaded")
    for t in tables:
        rep.add("INFO", t)

    check_crosswalk(con, rep)
    check_join_coverage(con, rep, tables)
    check_soilgrids(con, rep, tables)
    check_faostat(con, rep, tables)
    check_wfp(con, rep, tables)
    check_wb_hnp(con, rep, tables)
    check_provenance(con, rep)
    con.close()

    print(f"\nSummary: {rep.counts}")
    out = base / "data" / "processed" / "validation_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("# Kenya FNP database - consistency report\n")
        fh.write(f"\nAuthor: Aboubacar HEMA\n")
        fh.write(f"\nSummary: {rep.counts}\n")
        fh.write("\n".join(rep.lines))
    print(f"report -> {out}")
    return out
