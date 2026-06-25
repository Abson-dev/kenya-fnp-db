#!/usr/bin/env python3
"""Layer 5 (Policy) check.

Verifies the Action Plan tables, the county policy panel (fertilizer rollout
and the Policy Signal Index), and the CBIRR expenditure scaffold. Read-only;
needs no rebuild.

Run from the project root:  python check_policy.py
"""
from __future__ import annotations

from pathlib import Path

from kenyadb.checks import (CheckResult, columns, connect, coverage_check,
                            has_table, nonnull, provenance_summary,
                            schema_tables, table_check)

BASE = Path(__file__).resolve().parent


def run(base: Path = BASE) -> CheckResult:
    res = CheckResult("Policy")
    con = connect(base)

    # Action Plan structured tables (10 of them)
    if con is not None:
        ap = [t for t in schema_tables(con, "policy") if t.startswith("action_plan__")]
        (res.ok if len(ap) >= 10 else res.warn)("Action Plan tables", f"{len(ap)} found (expect 10)")
    else:
        res.skip("Action Plan tables", "database not built")

    # county policy panel
    summ = table_check(res, con, "policy", "policy_county_summary", expect=47)
    table_check(res, con, "policy", "policy_panel", expect=None)

    # fertilizer rollout merged, and the Policy Signal Index built
    if con is not None and summ:
        cols = columns(con, "policy", "policy_county_summary")
        if "fertilizer_priority" in cols:
            entered = nonnull(con, "policy", "policy_county_summary", "fertilizer_priority")
            by2023 = con.execute(
                "select count(*) from policy.policy_county_summary "
                "where fertilizer_priority >= 1").fetchone()[0]
            res.ok("fertilizer rollout", f"{entered}/47 keyed, {by2023} entered by 2023")
        else:
            res.warn("fertilizer rollout", "fertilizer_priority column absent")
        coverage_check(res, con, "policy", "policy_county_summary",
                       "policy_signal_index", expect=47, essential=False)

    # CBIRR expenditure scaffold (optional, awaiting CSVs)
    cob = base / "data" / "external" / "cob_expenditure"
    has_cob = cob.exists() and any(cob.glob("*.csv"))
    res.ok("CBIRR expenditure", "CSVs present") if has_cob \
        else res.info("CBIRR expenditure", "scaffolded, awaiting county CSVs (optional)")

    provenance_summary(res, con, "policy")
    if con is not None:
        con.close()
    return res


def main() -> None:
    run().print_report()


if __name__ == "__main__":
    main()
