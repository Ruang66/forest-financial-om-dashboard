"""Aggregation logic that powers the dashboard views."""
from datetime import date
from dateutil.relativedelta import relativedelta

from config import VAT_RATE, FY_START_MONTH
from contract_math import compute_contract, rent_at_month, invoice_amount_at_month, forward_12_months
from db import (
    fetchall, resolved_flag_keys_for_month, resolved_flags_for_month,
    all_rent_overrides,
)


def _load_all() -> list[dict]:
    rows = fetchall("SELECT * FROM contracts ORDER BY sheet_source, client_name")
    return [dict(r) for r in rows]


def _monthly_billable(contract: dict, for_month: date,
                      overrides: list[dict] | None = None) -> float:
    """Rent this contract bills in the given month, respecting status.

    Incomplete or internal (non-billable) contracts return 0.
    """
    if contract["status"] in ("incomplete", "internal_no_invoice"):
        return 0.0
    return rent_at_month(contract, for_month, overrides or [])


def _invoice_due_this_month(c: dict, today: date, overrides: list[dict]) -> bool:
    """True if this contract has a manual invoice due in today's month."""
    if c.get("invoice_type") != "Manually":
        return False
    if c["status"] not in ("active",):
        return False
    amount = invoice_amount_at_month(c, today.replace(day=1), overrides)
    return amount > 0


def _build_invoice_flags(enriched: list[dict], today: date, overrides_map: dict) -> tuple[list[dict], list[dict]]:
    """Returns (open_invoice_flags, resolved_invoice_flags) for this month."""
    current_month = today.strftime("%Y-%m")
    resolved_set = {cid for cid, ftype in resolved_flag_keys_for_month(current_month)
                    if ftype == "invoice_due"}

    open_flags = []
    for c in enriched:
        if not _invoice_due_this_month(c, today, overrides_map.get(c["id"], [])):
            continue
        if c["id"] in resolved_set:
            continue
        amount = invoice_amount_at_month(c, today.replace(day=1), overrides_map.get(c["id"], []))
        open_flags.append({
            **c,
            "flag_type": "invoice_due",
            "flag_month": current_month,
            "invoice_amount_due": amount,
        })

    open_flags.sort(key=lambda x: (x["sheet_source"], x["client_name"]))

    resolved_rows = [r for r in resolved_flags_for_month(current_month)
                     if r["flag_type"] == "invoice_due"]
    return open_flags, resolved_rows


def _convert_to_ex_vat(amount: float, vat_treatment: str) -> float:
    if vat_treatment == "inc_vat":
        return round(amount / (1 + VAT_RATE), 2)
    return amount


def _fy_bounds(today: date) -> tuple[date, date]:
    start_year = today.year if today.month >= FY_START_MONTH else today.year - 1
    start = date(start_year, FY_START_MONTH, 1)
    end = start + relativedelta(years=1)
    return start, end


def _enrich(contract: dict, today: date, overrides_map: dict[int, list[dict]]) -> dict:
    overrides = overrides_map.get(contract["id"], [])
    comp = compute_contract(contract, today, overrides)
    billable_now = _monthly_billable(contract, today.replace(day=1), overrides)
    return {
        **contract,
        "computed": comp,
        "overrides": overrides,
        "current_month_rent": billable_now,
        "current_month_rent_ex_vat": _convert_to_ex_vat(billable_now, contract["vat_treatment"]),
    }


def _month_key(d: date | None) -> str | None:
    return d.strftime("%Y-%m") if d else None


def _flags_for_contract(c: dict) -> list[tuple[str, str, str, date]]:
    """Returns a list of (flag_level, flag_type, flag_reason, flag_month_date).

    A single contract can fire multiple flags in the same month (e.g. an
    escalation AND a renewal decision). Each flag is keyed independently so
    resolving one does not hide the other.
    """
    comp = c["computed"]
    esc_pct = (c.get("escalation_pct") or 0) * 100
    notice_days = int(c.get("notice_period_days") or 60)
    out: list[tuple[str, str, str, date]] = []

    # End-of-contract flags only apply when not auto-renewing
    if not comp.auto_renew:
        if comp.is_escalating_this_month and comp.is_ending_this_month:
            out.append(("red", "escalation_and_end", "Escalation and contract end this month",
                        comp.next_escalation_date))
            return out  # combined supersedes the individual ones
        if comp.is_ending_this_month:
            out.append(("red", "end", "Contract ends this month", comp.end_date))
        elif comp.is_ending_next_month:
            out.append(("orange", "end", "Contract ends next month", comp.end_date))

    # Escalation flags fire for both auto-renew and fixed-term contracts
    if comp.is_escalating_this_month:
        out.append(("red", "escalation", f"Escalation this month (+{esc_pct:.1f}%)",
                    comp.next_escalation_date))
    elif comp.is_escalating_next_month:
        out.append(("orange", "escalation", f"Escalation next month (+{esc_pct:.1f}%)",
                    comp.next_escalation_date))

    # Renewal-decision flag only for auto-renew contracts
    if comp.auto_renew and comp.renewal_decision_date:
        if comp.is_renewal_decision_this_month:
            out.append(("red", "renewal_decision",
                        f"Renewal decision due ({notice_days} days notice before {comp.next_escalation_date.strftime('%d %b %Y')})",
                        comp.renewal_decision_date))
        elif comp.is_renewal_decision_next_month:
            out.append(("orange", "renewal_decision",
                        f"Renewal decision due next month ({notice_days} days notice before {comp.next_escalation_date.strftime('%d %b %Y')})",
                        comp.renewal_decision_date))

    return out


def _build_flags(enriched: list[dict], today: date) -> tuple[list[dict], list[dict]]:
    """Returns (open_flags, resolved_this_month_rows).

    Resolved state is keyed by flag_month (the month the escalation, end, or
    renewal decision falls in), so resolving an April 2026 flag does not
    suppress April 2027's.
    """
    month_keys: set[str] = set()
    candidate_flags: list[tuple[dict, tuple[str, str, str, date]]] = []
    for c in enriched:
        if c["status"] in ("incomplete", "internal_no_invoice"):
            continue
        for f in _flags_for_contract(c):
            _, _, _, flag_date = f
            month_keys.add(flag_date.strftime("%Y-%m"))
            candidate_flags.append((c, f))

    resolved_set: set[tuple[int, str, str]] = set()  # (contract_id, flag_type, month)
    for m in month_keys:
        for cid, ftype in resolved_flag_keys_for_month(m):
            resolved_set.add((cid, ftype, m))

    open_flags: list[dict] = []
    for c, (level, ftype, reason, flag_date) in candidate_flags:
        month = flag_date.strftime("%Y-%m")
        if (c["id"], ftype, month) in resolved_set:
            continue
        open_flags.append({
            **c,
            "flag_level": level,
            "flag_type": ftype,
            "flag_reason": reason,
            "flag_month": month,
        })

    # Red first, then orange. Within each level, sort by client name.
    open_flags.sort(key=lambda x: (0 if x["flag_level"] == "red" else 1, x["client_name"]))

    # Rows for "Resolved this month" panel: limit to current calendar month
    current_month = today.strftime("%Y-%m")
    next_month = (today.replace(day=1) + relativedelta(months=1)).strftime("%Y-%m")
    resolved_rows = (
        resolved_flags_for_month(current_month) + resolved_flags_for_month(next_month)
    )
    return open_flags, resolved_rows


def build_home(today: date | None = None) -> dict:
    """Powers the home / dashboard page (no forward roll-up)."""
    today = today or date.today()
    overrides_map = all_rent_overrides()
    contracts = [_enrich(c, today, overrides_map) for c in _load_all()]

    active = [c for c in contracts if c["status"] == "active"]
    commercial_active = [c for c in active if c["sheet_source"] == "commercial"]
    residential_active = [c for c in active if c["sheet_source"] == "residential"]
    incomplete = [c for c in contracts if c["status"] == "incomplete"]
    internal = [c for c in contracts if c["status"] == "internal_no_invoice"]

    commercial_monthly_ex = sum(c["current_month_rent"] for c in commercial_active)
    residential_monthly_inc = sum(c["current_month_rent"] for c in residential_active)
    residential_monthly_ex = sum(c["current_month_rent_ex_vat"] for c in residential_active)
    total_monthly_ex = commercial_monthly_ex + residential_monthly_ex
    total_annual_ex = total_monthly_ex * 12

    open_flags, resolved_rows = _build_flags(contracts, today)
    invoice_flags, resolved_invoice_rows = _build_invoice_flags(contracts, today, overrides_map)

    fy_start, fy_end = _fy_bounds(today)
    fy_months = []
    cursor = fy_start
    while cursor < fy_end:
        fy_months.append(cursor)
        cursor += relativedelta(months=1)
    fy_forecast_ex = 0.0
    for m in fy_months:
        for c in active:
            r = _monthly_billable(c, m, overrides_map.get(c["id"], []))
            fy_forecast_ex += _convert_to_ex_vat(r, c["vat_treatment"])

    return {
        "today": today,
        "fy_label": f"FY{str(fy_end.year)[-2:]}",
        "fy_start": fy_start,
        "fy_end": fy_end - relativedelta(days=1),
        "fy_forecast_ex_vat": fy_forecast_ex,
        "kpis": {
            "total_sites": len(active),
            "commercial_sites": len(commercial_active),
            "residential_sites": len(residential_active),
            "incomplete_count": len(incomplete),
            "internal_count": len(internal),
            "commercial_monthly_ex": commercial_monthly_ex,
            "residential_monthly_inc": residential_monthly_inc,
            "residential_monthly_ex": residential_monthly_ex,
            "total_monthly_ex": total_monthly_ex,
            "total_annual_ex": total_annual_ex,
        },
        "flags": open_flags,
        "resolved_flags": resolved_rows,
        "invoice_flags": invoice_flags,
        "resolved_invoice_flags": resolved_invoice_rows,
        "incomplete": incomplete,
        "internal": internal,
    }


def build_forecast(today: date | None = None) -> dict:
    """Powers the forward 12 month revenue page."""
    today = today or date.today()
    overrides_map = all_rent_overrides()
    contracts = [_enrich(c, today, overrides_map) for c in _load_all()]
    active = [c for c in contracts if c["status"] == "active"]

    months = forward_12_months(today)
    per_month_total_ex = []
    per_month_commercial_ex = []
    per_month_residential_ex = []
    for m in months:
        c_total = r_total = 0.0
        for c in active:
            if c["status"] in ("incomplete", "internal_no_invoice"):
                continue
            inv = invoice_amount_at_month(c, m, overrides_map.get(c["id"], []))
            ex = _convert_to_ex_vat(inv, c["vat_treatment"])
            if c["sheet_source"] == "commercial":
                c_total += ex
            else:
                r_total += ex
        per_month_commercial_ex.append(c_total)
        per_month_residential_ex.append(r_total)
        per_month_total_ex.append(c_total + r_total)

    forward_rows = []
    for c in active:
        overrides = overrides_map.get(c["id"], [])
        vals = [invoice_amount_at_month(c, m, overrides) for m in months]
        if any(v > 0 for v in vals):
            forward_rows.append({
                "id": c["id"],
                "client_name": c["client_name"],
                "project_number": c["project_number"],
                "sheet_source": c["sheet_source"],
                "vat_treatment": c["vat_treatment"],
                "invoice_frequency": c.get("invoice_frequency", "monthly"),
                "monthly_values": vals,
                "total": sum(vals),
            })
    forward_rows.sort(key=lambda x: (x["sheet_source"], x["client_name"]))

    return {
        "today": today,
        "months": months,
        "per_month_total_ex": per_month_total_ex,
        "per_month_commercial_ex": per_month_commercial_ex,
        "per_month_residential_ex": per_month_residential_ex,
        "rows": forward_rows,
    }


def build_category(source: str, today: date | None = None) -> dict:
    """Powers the /commercial and /residential pages.

    `source` must be 'commercial' or 'residential'.
    """
    assert source in ("commercial", "residential")
    today = today or date.today()
    overrides_map = all_rent_overrides()
    contracts = [_enrich(c, today, overrides_map) for c in _load_all()]
    category = [c for c in contracts if c["sheet_source"] == source]
    active_cat = [c for c in category if c["status"] == "active"]

    monthly_native = sum(c["current_month_rent"] for c in active_cat)
    monthly_ex = sum(c["current_month_rent_ex_vat"] for c in active_cat)
    annual_ex = monthly_ex * 12

    # Flags: only for this category
    cat_contracts = [c for c in contracts if c["sheet_source"] == source]
    open_flags, resolved_rows = _build_flags(cat_contracts, today)
    invoice_flags, resolved_invoice_rows = _build_invoice_flags(cat_contracts, today, overrides_map)

    # Incomplete rows for this category
    incomplete = [c for c in category if c["status"] == "incomplete"]
    internal = [c for c in category if c["status"] == "internal_no_invoice"]

    return {
        "today": today,
        "source": source,
        "title": "Commercial O&M Plants" if source == "commercial" else "Residential PPA",
        "vat_note": "Ex VAT" if source == "commercial" else "Inc VAT (converted Ex VAT for totals)",
        "kpis": {
            "total_sites": len(active_cat),
            "incomplete_count": len(incomplete),
            "internal_count": len(internal),
            "monthly_native": monthly_native,
            "monthly_ex": monthly_ex,
            "annual_ex": annual_ex,
        },
        "contracts": category,
        "active": active_cat,
        "incomplete": incomplete,
        "internal": internal,
        "flags": open_flags,
        "resolved_flags": resolved_rows,
        "invoice_flags": invoice_flags,
        "resolved_invoice_flags": resolved_invoice_rows,
    }
