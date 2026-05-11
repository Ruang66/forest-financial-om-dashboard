"""SQLite access layer. Keeps the rest of the app free of SQL."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable

from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_source         TEXT    NOT NULL CHECK (sheet_source IN ('commercial','residential')),
    project_number       TEXT,
    client_name          TEXT    NOT NULL,
    contact_person       TEXT,
    client_email         TEXT,
    invoice_type         TEXT,                  -- Manually / Automatic / No Invoice
    start_date           TEXT,                  -- ISO YYYY-MM-DD, nullable for incomplete imports
    contract_term_years  REAL,                  -- Initial term, used only when auto_renew=0
    base_monthly_rent    REAL,                  -- Ex VAT for commercial, Inc VAT for residential
    escalation_pct       REAL,                  -- e.g. 0.065 for 6.5%
    vat_treatment        TEXT    NOT NULL CHECK (vat_treatment IN ('ex_vat','inc_vat')),
    auto_renew           INTEGER NOT NULL DEFAULT 1,    -- 1 = no hard end date, anniversary escalations forever
    notice_period_days   INTEGER NOT NULL DEFAULT 60,   -- Lead time for renewal-decision warning flag
    status               TEXT    NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active','incomplete','internal_no_invoice','ended')),
    notes                TEXT,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);
CREATE INDEX IF NOT EXISTS idx_contracts_sheet  ON contracts(sheet_source);
CREATE INDEX IF NOT EXISTS idx_contracts_start  ON contracts(start_date);

-- Resolved "Action Required" flags. One row per contract + flag type + flag month.
-- Nothing auto-resolves or auto-expires. Users resolve manually via the dashboard.
CREATE TABLE IF NOT EXISTS flag_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id   INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    flag_type     TEXT    NOT NULL CHECK (flag_type IN ('escalation','end','escalation_and_end','renewal_decision')),
    flag_month    TEXT    NOT NULL,                 -- YYYY-MM
    resolved_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_by   TEXT,                             -- reserved for when auth is added
    UNIQUE (contract_id, flag_type, flag_month)
);
CREATE INDEX IF NOT EXISTS idx_flag_actions_month ON flag_actions(flag_month);

-- Manually entered rent overrides. Used when actual escalation differs
-- from the contracted percentage (negotiated rate, CPI delta, rent holiday).
-- One row per contract + effective_date.
CREATE TABLE IF NOT EXISTS rent_overrides (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id     INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    effective_date  TEXT    NOT NULL,                  -- ISO YYYY-MM-DD, typically the anniversary
    override_rent   REAL    NOT NULL,                  -- Same VAT treatment as base_monthly_rent
    reason          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (contract_id, effective_date)
);
CREATE INDEX IF NOT EXISTS idx_rent_overrides_contract ON rent_overrides(contract_id);
"""


def _migrate_legacy_db(c: sqlite3.Connection) -> None:
    """Apply column adds for older DBs (created before auto_renew was a thing)."""
    cols = {row["name"] for row in c.execute("PRAGMA table_info(contracts)")}
    if "auto_renew" not in cols:
        c.execute("ALTER TABLE contracts ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 1")
    if "notice_period_days" not in cols:
        c.execute("ALTER TABLE contracts ADD COLUMN notice_period_days INTEGER NOT NULL DEFAULT 60")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate_legacy_db(c)


def fetchall(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(sql, params).fetchall()


def fetchone(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(sql, params).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid or cur.rowcount


CONTRACT_COLS = [
    "sheet_source", "project_number", "client_name", "contact_person",
    "client_email", "invoice_type", "start_date", "contract_term_years",
    "base_monthly_rent", "escalation_pct", "vat_treatment",
    "auto_renew", "notice_period_days",
    "status", "notes",
]


def insert_contract(data: dict) -> int:
    placeholders = ",".join("?" * len(CONTRACT_COLS))
    sql = f"INSERT INTO contracts ({','.join(CONTRACT_COLS)}) VALUES ({placeholders})"
    # Default auto_renew = 1 and notice_period_days = 60 if caller omits them.
    values = []
    for k in CONTRACT_COLS:
        v = data.get(k)
        if v is None and k == "auto_renew":
            v = 1
        if v is None and k == "notice_period_days":
            v = 60
        values.append(v)
    with conn() as c:
        cur = c.execute(sql, values)
        return cur.lastrowid


def update_contract(contract_id: int, data: dict) -> None:
    set_clause = ", ".join(f"{c} = ?" for c in CONTRACT_COLS)
    sql = f"UPDATE contracts SET {set_clause}, updated_at = ? WHERE id = ?"
    params = [data.get(k) for k in CONTRACT_COLS] + [
        datetime.utcnow().isoformat(timespec="seconds"), contract_id,
    ]
    with conn() as c:
        c.execute(sql, params)


def delete_contract(contract_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))


# ---- flag actions -----------------------------------------------------------

def resolve_flag(contract_id: int, flag_type: str, flag_month: str, by: str | None = None) -> None:
    """Mark a flag as actioned. Idempotent on the unique key."""
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO flag_actions (contract_id, flag_type, flag_month, resolved_by) VALUES (?, ?, ?, ?)",
            (contract_id, flag_type, flag_month, by),
        )


def unresolve_flag(contract_id: int, flag_type: str, flag_month: str) -> None:
    with conn() as c:
        c.execute(
            "DELETE FROM flag_actions WHERE contract_id = ? AND flag_type = ? AND flag_month = ?",
            (contract_id, flag_type, flag_month),
        )


def resolved_flag_keys_for_month(flag_month: str) -> set[tuple[int, str]]:
    """Returns the set of (contract_id, flag_type) already resolved for the given month."""
    rows = fetchall(
        "SELECT contract_id, flag_type FROM flag_actions WHERE flag_month = ?",
        (flag_month,),
    )
    return {(r["contract_id"], r["flag_type"]) for r in rows}


def resolved_flags_for_month(flag_month: str) -> list[dict]:
    """Full rows of resolved flags for a given month, for the 'Resolved this month' panel."""
    rows = fetchall(
        """
        SELECT f.*, c.client_name, c.project_number, c.sheet_source
        FROM flag_actions f
        JOIN contracts c ON c.id = f.contract_id
        WHERE f.flag_month = ?
        ORDER BY f.resolved_at DESC
        """,
        (flag_month,),
    )
    return [dict(r) for r in rows]


# ---- rent overrides --------------------------------------------------------

def insert_rent_override(contract_id: int, effective_date: str,
                         override_rent: float, reason: str | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO rent_overrides (contract_id, effective_date, override_rent, reason) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(contract_id, effective_date) DO UPDATE SET override_rent=excluded.override_rent, reason=excluded.reason",
            (contract_id, effective_date, override_rent, reason),
        )
        return cur.lastrowid


def delete_rent_override(override_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM rent_overrides WHERE id = ?", (override_id,))


def rent_overrides_for_contract(contract_id: int) -> list[dict]:
    """Returns all overrides for a contract, sorted by effective_date ascending."""
    rows = fetchall(
        "SELECT * FROM rent_overrides WHERE contract_id = ? ORDER BY effective_date ASC",
        (contract_id,),
    )
    return [dict(r) for r in rows]


def all_rent_overrides() -> dict[int, list[dict]]:
    """Returns a {contract_id: [override dicts]} map, sorted by effective_date asc.

    Used by the dashboard service to apply overrides while computing rent for many
    contracts in one pass.
    """
    rows = fetchall(
        "SELECT * FROM rent_overrides ORDER BY contract_id, effective_date ASC"
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["contract_id"], []).append(dict(r))
    return out
