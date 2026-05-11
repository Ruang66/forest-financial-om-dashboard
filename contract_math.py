"""Contract math: escalations, rent timeline, end date, forward roll-up.

All calculations here are deterministic functions of the stored fields, so the
dashboard is always computed from the source data and never from cached values.

Rent timeline rules:
- The contract starts at base_monthly_rent on start_date.
- On each anniversary of start_date, rent escalates by escalation_pct,
  unless a rent_override row exists with that anniversary as its effective_date,
  in which case the override_rent replaces what the math would have produced.
- Subsequent escalations compound from whichever value held last (override or computed).
- If auto_renew = 0, the contract ends at start_date + contract_term_years (rent goes to 0).
- If auto_renew = 1, there is no hard end date. Anniversaries continue forever.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta


def _parse_date(val) -> Optional[date]:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
    return None


@dataclass
class ContractComputed:
    current_monthly_rent: float
    next_monthly_rent: float
    next_escalation_date: Optional[date]
    end_date: Optional[date]              # None if auto_renew or term not set
    years_elapsed: int
    months_to_end: Optional[int]
    is_escalating_this_month: bool
    is_ending_this_month: bool
    is_escalating_next_month: bool
    is_ending_next_month: bool
    contract_ended: bool
    auto_renew: bool
    renewal_decision_date: Optional[date]            # next_anniversary - notice_period_days
    is_renewal_decision_this_month: bool
    is_renewal_decision_next_month: bool


def years_elapsed(start: date, today: date) -> int:
    """Completed whole years between start and today."""
    years = today.year - start.year
    if (today.month, today.day) < (start.month, start.day):
        years -= 1
    return max(0, years)


def _rent_at_anniversary(base: float, esc_pct: float, anniversary_index: int,
                         start: date, overrides: list[dict]) -> float:
    """Rent for the year STARTING at start + anniversary_index years.

    Walks forward from anniversary 0 (= base) and applies either the override
    (if the override's effective_date matches that anniversary) or the standard
    escalation. Applying overrides in order means subsequent escalations
    compound off the override value.
    """
    if anniversary_index <= 0:
        # Year 0 (initial term) is the base rent unless an override is dated
        # exactly on start_date itself.
        for o in overrides:
            if _parse_date(o["effective_date"]) == start:
                return float(o["override_rent"])
        return base

    # Build a quick lookup of overrides keyed by anniversary index (1-based)
    by_index: dict[int, float] = {}
    for o in overrides:
        eff = _parse_date(o["effective_date"])
        if not eff:
            continue
        idx = eff.year - start.year
        # Only count overrides whose effective_date is on the start month/day
        if eff == start + relativedelta(years=idx) and idx >= 0:
            by_index[idx] = float(o["override_rent"])

    rent = by_index.get(0, base)
    for i in range(1, anniversary_index + 1):
        if i in by_index:
            rent = by_index[i]
        else:
            rent = rent * (1 + esc_pct)
    return round(rent, 2)


def compute_contract(c: dict, today: Optional[date] = None,
                     overrides: Optional[list[dict]] = None) -> ContractComputed:
    """Build the computed view for a single contract row (dict-like).

    Expected keys: start_date, contract_term_years, base_monthly_rent,
    escalation_pct, auto_renew, notice_period_days.
    overrides: optional list of {effective_date, override_rent} dicts for THIS contract.
    """
    today = today or date.today()
    overrides = overrides or []
    start = _parse_date(c.get("start_date"))
    term = c.get("contract_term_years") or 0
    base = c.get("base_monthly_rent") or 0
    esc = c.get("escalation_pct") or 0
    auto_renew = bool(c.get("auto_renew", 1))
    notice_days = int(c.get("notice_period_days") or 60)

    if start is None:
        return ContractComputed(
            current_monthly_rent=base or 0,
            next_monthly_rent=base or 0,
            next_escalation_date=None,
            end_date=None,
            years_elapsed=0,
            months_to_end=None,
            is_escalating_this_month=False,
            is_ending_this_month=False,
            is_escalating_next_month=False,
            is_ending_next_month=False,
            contract_ended=False,
            auto_renew=auto_renew,
            renewal_decision_date=None,
            is_renewal_decision_this_month=False,
            is_renewal_decision_next_month=False,
        )

    y = years_elapsed(start, today)
    current_rent = _rent_at_anniversary(base, esc, y, start, overrides)
    next_rent = _rent_at_anniversary(base, esc, y + 1, start, overrides)

    next_esc = start + relativedelta(years=y + 1)

    # Hard end date only when not auto-renewing
    end_dt: Optional[date] = None
    if not auto_renew and term:
        end_dt = start + relativedelta(years=int(term))

    # Current calendar month flags
    this_year, this_month = today.year, today.month
    next_dt = (today.replace(day=1) + relativedelta(months=1))
    next_year, next_month = next_dt.year, next_dt.month

    esc_this = (next_esc.year, next_esc.month) == (this_year, this_month)
    esc_next = (next_esc.year, next_esc.month) == (next_year, next_month)

    end_this = end_dt is not None and (end_dt.year, end_dt.month) == (this_year, this_month)
    end_next = end_dt is not None and (end_dt.year, end_dt.month) == (next_year, next_month)

    contract_ended = end_dt is not None and end_dt < today.replace(day=1)

    months_to_end = None
    if end_dt:
        months_to_end = (end_dt.year - today.year) * 12 + (end_dt.month - today.month)

    # Renewal decision: lead-time warning before each anniversary, only meaningful for auto-renew
    renewal_decision_dt: Optional[date] = None
    rd_this = rd_next = False
    if auto_renew:
        renewal_decision_dt = next_esc - timedelta(days=notice_days)
        rd_this = (renewal_decision_dt.year, renewal_decision_dt.month) == (this_year, this_month)
        rd_next = (renewal_decision_dt.year, renewal_decision_dt.month) == (next_year, next_month)

    return ContractComputed(
        current_monthly_rent=current_rent,
        next_monthly_rent=next_rent,
        next_escalation_date=next_esc,
        end_date=end_dt,
        years_elapsed=y,
        months_to_end=months_to_end,
        is_escalating_this_month=esc_this,
        is_ending_this_month=end_this,
        is_escalating_next_month=esc_next,
        is_ending_next_month=end_next,
        contract_ended=contract_ended,
        auto_renew=auto_renew,
        renewal_decision_date=renewal_decision_dt,
        is_renewal_decision_this_month=rd_this,
        is_renewal_decision_next_month=rd_next,
    )


def rent_at_month(c: dict, target_month: date,
                  overrides: Optional[list[dict]] = None) -> float:
    """Rent this contract bills in a given month, applying all prior escalations
    (and overrides). Returns 0 if not yet started or already ended."""
    overrides = overrides or []
    start = _parse_date(c.get("start_date"))
    if start is None:
        return 0.0

    target = target_month.replace(day=1)
    start_m = start.replace(day=1)
    if target < start_m:
        return 0.0

    auto_renew = bool(c.get("auto_renew", 1))
    term = c.get("contract_term_years") or 0
    if not auto_renew and term:
        end_dt = start + relativedelta(years=int(term))
        if target >= end_dt.replace(day=1):
            return 0.0

    base = c.get("base_monthly_rent") or 0
    esc = c.get("escalation_pct") or 0
    y = years_elapsed(start, target)
    return _rent_at_anniversary(base, esc, y, start, overrides)


def forward_12_months(today: Optional[date] = None) -> list[date]:
    """List of 12 first-of-month dates starting with the current month."""
    today = today or date.today()
    first = today.replace(day=1)
    return [first + relativedelta(months=i) for i in range(12)]
