#!/usr/bin/env python3
"""
Fetch financial-statement data straight from SEC EDGAR (free, primary source) for every
ticker in universe.txt, and write it in the same schema rh_adapter.py already understands.

Runs in GitHub Actions (open internet). Claude's cloud workspace cannot reach sec.gov,
so this job is what gets the data in.

Output: edgar_data/<YYYY-MM-DD>.json and edgar_data/latest.json
Only financial-statement fields are filled here. Market fields (price, market cap,
shares, P/E, sector, year-end prices) come from Robinhood in the Sunday run and are
merged by merge_data.py.

Rules (same as the rest of the screener):
- Consolidated facts only. (EDGAR's companyfacts feed contains no segment/dimension facts.)
- Every concept used, including fallbacks, is written to the ticker's notes.
- Missing stays missing (null). Nothing is estimated.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
UA = os.environ.get("SEC_USER_AGENT") or "stock-screener personal research (github.com/anveshragi/stock-screener-data)"
B = 1e9

# Ordered fallbacks: first concept with data wins. Changing these lists changes data,
# not rules, but log every change in the commit message.
FLOWS = {
    "op_income":        ["OperatingIncomeLoss"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
                         "InterestAndDebtExpense", "InterestExpenseAggregateExpenses",
                         "InterestPaidNet"],  # last resort: cash interest paid (usually >= expense; noted)
    "ocf":              ["NetCashProvidedByUsedInOperatingActivities",
                         "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex":            ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                         "PaymentsForCapitalImprovements", "PaymentsToAcquireRealEstate",
                         "PaymentsToAcquireOtherProductiveAssets",
                         "_OilGasPlusOtherPPE", "PaymentsToAcquireOilAndGasPropertyAndEquipment"],
    "pretax":           ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                         "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                         "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
    "tax":              ["IncomeTaxExpenseBenefit"],
}
# Domestic-only pretax is NOT total pretax; only the domestic + foreign SUM is used.
FLOWS["pretax"] = FLOWS["pretax"][:2] + ["_PretaxDomesticPlusForeign"]

# Synthetic concepts: exact sums of parts the company tags separately (same period only).
SYNTHETIC = {
    "_PretaxDomesticPlusForeign": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
                                   "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign"],
    "_OilGasPlusOtherPPE": ["PaymentsToAcquireOilAndGasPropertyAndEquipment",
                            "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
}

ANNUAL_SERIES = {
    "revenue":    ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                   "RegulatedAndUnregulatedOperatingRevenue", "RevenueFromContractWithCustomerIncludingAssessedTax",
                   "RevenuesNetOfInterestExpense"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
}
BALANCE = {
    "current_assets":      ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "total_assets":        ["Assets"],
    "total_liabilities":   ["Liabilities"],
    "retained_earnings":   ["RetainedEarningsAccumulatedDeficit"],
    "equity_total":        ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                            "StockholdersEquity",
                            "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
                            "PartnersCapital"],
    "cash":                ["CashAndCashEquivalentsAtCarryingValue",
                            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],  # incl. restricted; noted
}
DEBT = ["LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities", "DebtAndCapitalLeaseObligations",
        "LongTermDebt",
        "LongTermDebtNoncurrent", "LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings",
        "CommercialPaper", "ConvertibleDebtNoncurrent", "ConvertibleNotesPayable",
        "LongTermDebtAndCapitalLeaseObligations"]


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def d(s):
    return dt.date.fromisoformat(s)


def usd_rows(facts, concept):
    # companyfacts layout: {"cik", "entityName", "facts": {"us-gaap": {concept: {"units": {"USD": [...]}}}}}
    node = facts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    rows = node.get("units", {}).get("USD", [])
    return [r for r in rows if r.get("form") in ("10-K", "10-Q", "10-K/A", "10-Q/A")]


def latest_filed(rows):
    """Keep one value per (start,end): the most recently filed (handles restatements)."""
    best = {}
    for r in rows:
        k = (r.get("start"), r["end"])
        if k not in best or r["filed"] > best[k]["filed"]:
            best[k] = r
    return list(best.values())


def duration(r):
    return (d(r["end"]) - d(r["start"])).days if r.get("start") else None


def annuals(rows):
    out = [r for r in latest_filed(rows) if r.get("start") and 350 <= duration(r) <= 380]
    return sorted(out, key=lambda r: r["end"], reverse=True)


def flow(facts, concepts, q_end, k_end):
    """Return ({fy, ytd, ytd_prior}, [concepts used]). FY and the YTD pair are each taken
    from the first concept in the list that has them (companies sometimes switch tags)."""
    out, used = {"fy": None, "ytd": None, "ytd_prior": None}, []
    for c in concepts:
        rows = latest_filed(usd_rows(facts, c))
        if not rows:
            continue
        if out["fy"] is None and k_end:
            fy_row = next((r for r in annuals(rows) if r["end"] == k_end), None)
            if fy_row:
                out["fy"] = fy_row["val"] / B
                used.append(c)
        if out["ytd"] is None and q_end and k_end and q_end > k_end:
            cands = [r for r in rows if r["end"] == q_end and r.get("start") and 80 <= duration(r) < 340
                     and d(r["start"]) > d(k_end)]
            if cands:
                cur = max(cands, key=duration)
                ln = duration(cur)
                target = d(cur["end"]) - dt.timedelta(days=364)
                prior = [r for r in rows if r.get("start") and abs(duration(r) - ln) <= 10
                         and abs((d(r["end"]) - target).days) <= 10]
                if prior:
                    out["ytd"], out["ytd_prior"] = cur["val"] / B, prior[0]["val"] / B
                    if c not in used:
                        used.append(c)
    return out, used


def add_synthetic(facts):
    """Add each SYNTHETIC concept as the sum of its parts, for periods where all parts exist."""
    gaap = facts.setdefault("facts", {}).setdefault("us-gaap", {})
    for name, parts in SYNTHETIC.items():
        tables = [{(r.get("start"), r["end"]): r for r in latest_filed(usd_rows(facts, p))} for p in parts]
        rows = []
        for k in set(tables[0]).intersection(*tables[1:]):
            rs = [t[k] for t in tables]
            latest = max(rs, key=lambda r: r["filed"])
            rows.append(dict(latest, val=sum(r["val"] for r in rs)))
        if rows:
            gaap[name] = {"units": {"USD": rows}}


def instant(facts, concepts, at):
    for c in concepts:
        rows = [r for r in latest_filed(usd_rows(facts, c)) if not r.get("start") and r["end"] == at]
        if rows:
            return rows[0]["val"] / B, c
    return None, None


def concept_inventory(facts):
    """Diagnostic: every USD us-gaap concept with 10-K/10-Q data ending 2024 or later.
    concept -> [latest end, start or "", value in $bn, form, filed]. Used to fix concept mappings."""
    inv = {}
    for c in facts.get("facts", {}).get("us-gaap", {}):
        rows = [r for r in usd_rows(facts, c) if r["end"] >= "2024-01-01"]
        if rows:
            r = max(rows, key=lambda r: (r["end"], r["filed"], duration(r) or 0))
            inv[c] = [r["end"], r.get("start", ""), round(r["val"] / B, 4), r["form"], r["filed"]]
    return inv


def build(ticker, cik, facts):
    notes = []
    add_synthetic(facts)
    # Period anchors: latest 10-K fiscal year end, latest 10-Q period end.
    probe = usd_rows(facts, "Assets") or usd_rows(facts, "Liabilities") or []
    k_ends = sorted({r["end"] for r in probe if r["form"].startswith("10-K")})
    q_ends = sorted({r["end"] for r in probe if r["form"].startswith("10-Q")})
    k_end = k_ends[-1] if k_ends else None
    q_end = q_ends[-1] if q_ends else None
    if not k_end and not q_end:
        return {"name": facts.get("entityName", ""), "unavailable": "EDGAR has no 10-K/10-Q balance sheet facts"}
    bal_at = max(e for e in (k_end, q_end) if e)

    out = {"name": facts.get("entityName", ""), "cik": cik, "edgar_k_end": k_end, "edgar_q_end": q_end,
           "ytd_label": f"YTD to {q_end}" if (q_end and k_end and q_end > k_end) else f"FY to {k_end}",
           "flows": {}, "balance": {}, "debt_facts": {}, "debt_facts_annual": {}}

    for key, concepts in FLOWS.items():
        out["flows"][key], used = flow(facts, concepts, q_end, k_end)
        if used and used != [concepts[0]]:
            notes.append(f"{key} = {' / '.join(used)}")
        if not used:
            notes.append(f"{key}: none of the mapped concepts tagged")

    for key, concepts in ANNUAL_SERIES.items():
        # Build one value per fiscal year: for each year, the first concept in priority order
        # that reports it. (Companies switch tags over time; a tag that stopped years ago
        # must never supply "latest" figures.) Only years up to the latest 10-K count.
        by_end, src = {}, {}
        for c in concepts:
            for r in annuals(usd_rows(facts, c)):
                if k_end and r["end"] > k_end:
                    continue
                if r["end"] not in by_end:
                    by_end[r["end"]], src[r["end"]] = r["val"] / B, c
        ends = sorted(by_end, reverse=True)[:5]
        if ends and k_end and ends[0] != k_end:
            notes.append(f"{key}: latest fiscal year ({k_end}) not tagged; series left empty")
            ends = []
        out[key] = [by_end[e] for e in ends]
        out[f"{key}_years"] = ends
        used = sorted({src[e] for e in ends})
        if used and used != [concepts[0]]:
            notes.append(f"{key} = {' + '.join(used)}")

    for key, concepts in BALANCE.items():
        v, used = instant(facts, concepts, bal_at)
        if v is None and k_end and k_end != bal_at:
            v, used = instant(facts, concepts, k_end)
            if used:
                notes.append(f"{key}: from last annual report ({k_end}); not in latest filing")
        out["balance"][key] = v
        if used and used != concepts[0]:
            notes.append(f"{key} = {used}")

    def debt_at(at):
        found = {}
        for c in DEBT:
            v, _ = instant(facts, [c], at)
            if v is not None:
                found[c] = v
        return found
    out["debt_facts"] = debt_at(bal_at)
    if not out["debt_facts"] and k_end and k_end != bal_at:
        out["debt_facts"] = debt_at(k_end)
        if out["debt_facts"]:
            notes.append(f"debt: from last annual report ({k_end}); not in latest filing")
    if k_end:
        va, _ = instant(facts, ["LongTermDebtNoncurrent"], k_end)
        if va is not None:
            out["debt_facts_annual"]["LongTermDebtNoncurrent"] = va
    out["notes"] = ["source: SEC EDGAR companyfacts"] + notes
    return out


def main():
    tickers = [t.strip().upper() for t in (HERE / "universe.txt").read_text().split() if t.strip()]
    if "@" not in UA:
        print("WARNING: SEC_USER_AGENT secret not set. SEC requires a name + contact email in the "
              "User-Agent and returns 403 Forbidden without one.")
    try:
        tick = get_json("https://www.sec.gov/files/company_tickers.json")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            sys.exit("SEC returned 403 Forbidden. Fix: repo Settings -> Secrets and variables -> Actions -> "
                     "New repository secret, name SEC_USER_AGENT, value e.g. 'Your Name you@example.com'.")
        raise
    cikmap = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in tick.values()}
    result = {"_about": f"SEC EDGAR companyfacts pulled {dt.date.today().isoformat()}. USD billions. "
                        "Financial-statement fields only; market fields merged later from Robinhood."}
    ok = 0
    inventory = {}
    for t in tickers:
        cik = cikmap.get(t.replace(".", "-")) or cikmap.get(t)
        if not cik:
            result[t] = {"unavailable": "ticker not in SEC company_tickers.json"}
            continue
        try:
            facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
            inventory[t] = concept_inventory(facts)
            result[t] = build(t, cik, facts)
            ok += "unavailable" not in result[t]
        except Exception as e:  # keep going; record the failure
            result[t] = {"unavailable": f"EDGAR fetch failed: {e}"}
        time.sleep(0.15)  # SEC fair-access limit is 10 requests/second
    outdir = HERE / "edgar_data"
    outdir.mkdir(exist_ok=True)
    text = json.dumps(result, indent=1)
    (outdir / f"{dt.date.today().isoformat()}.json").write_text(text)
    (outdir / "latest.json").write_text(text)
    (outdir / "concepts.json").write_text(json.dumps(inventory, indent=0, sort_keys=True))
    print(f"EDGAR: {ok}/{len(tickers)} tickers with data")
    for t in tickers:
        if "unavailable" in result[t]:
            print(f"  missing {t}: {result[t]['unavailable']}")
    if ok < len(tickers) // 2:  # something systemic broke; fail the job rather than commit junk
        sys.exit(f"FAILED: only {ok}/{len(tickers)} tickers parsed; not committing.")


if __name__ == "__main__":
    sys.exit(main())
