"""Postgres access layer."""
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable

import psycopg2
import psycopg2.extras

def _get_dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    database = os.environ.get("PGDATABASE", "railway")
    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"

DATABASE_URL = _get_dsn()

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS contracts (
        id                   SERIAL PRIMARY KEY,
        sheet_source         TEXT    NOT NULL CHECK (sheet_source IN ('commercial','residential')),
        project_number       TEXT,
        client_name          TEXT    NOT NULL,
        contact_person       TEXT,
        client_email         TEXT,
        invoice_type         TEXT,
        invoice_frequency    TEXT    NOT NULL DEFAULT 'monthly'
                                     CHECK (invoice_frequency IN ('monthly','quarterly','annually')),
        start_date           TEXT,
        contract_term_years  REAL,
        base_monthly_rent    REAL,
        escalation_pct       REAL,
        vat_treatment        TEXT    NOT NULL CHECK (vat_treatment IN ('ex_vat','inc_vat')),
        auto_renew           INTEGER NOT NULL DEFAULT 1,
        notice_period_days   INTEGER NOT NULL DEFAULT 60,
        status               TEXT    NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active','incomplete','internal_no_invoice','ended')),
        notes                TEXT,
        created_at           TEXT    NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
        updated_at           TEXT    NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
    )""",
    "CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status)",
    "CREATE INDEX IF NOT EXISTS idx_contracts_sheet  ON contracts(sheet_source)",
    "CREATE INDEX IF NOT EXISTS idx_contracts_start  ON contracts(start_date)",
    """CREATE TABLE IF NOT EXISTS flag_actions (
        id            SERIAL PRIMARY KEY,
        contract_id   INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        flag_type     TEXT    NOT NULL CHECK (flag_type IN ('escalation','end','escalation_and_end','renewal_decision')),
        flag_month    TEXT    NOT NULL,
        resolved_at   TEXT    NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
        resolved_by   TEXT,
        UNIQUE (contract_id, flag_type, flag_month)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_flag_actions_month ON flag_actions(flag_month)",
    """CREATE TABLE IF NOT EXISTS rent_overrides (
        id              SERIAL PRIMARY KEY,
        contract_id     INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        effective_date  TEXT    NOT NULL,
        override_rent   REAL    NOT NULL,
        reason          TEXT,
        created_at      TEXT    NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
        UNIQUE (contract_id, effective_date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rent_overrides_contract ON rent_overrides(contract_id)",
]


@contextmanager
def conn():
    c = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        cur = c.cursor()
        for stmt in _SCHEMA:
            cur.execute(stmt)
        _migrate_legacy(cur)


def _migrate_legacy(cur) -> None:
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'contracts' AND table_schema = 'public'
    """)
    cols = {row["column_name"] for row in cur.fetchall()}
    if "auto_renew" not in cols:
        cur.execute("ALTER TABLE contracts ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 1")
    if "notice_period_days" not in cols:
        cur.execute("ALTER TABLE contracts ADD COLUMN notice_period_days INTEGER NOT NULL DEFAULT 60")
    if "invoice_frequency" not in cols:
        cur.execute("ALTER TABLE contracts ADD COLUMN invoice_frequency TEXT NOT NULL DEFAULT 'monthly'")


def fetchall(sql: str, params: Iterable[Any] = ()) -> list:
    with conn() as c:
        cur = c.cursor()
        cur.execute(sql, list(params))
        return cur.fetchall()


def fetchone(sql: str, params: Iterable[Any] = ()):
    with conn() as c:
        cur = c.cursor()
        cur.execute(sql, list(params))
        return cur.fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with conn() as c:
        cur = c.cursor()
        cur.execute(sql, list(params))
        return cur.rowcount


CONTRACT_COLS = [
    "sheet_source", "project_number", "client_name", "contact_person",
    "client_email", "invoice_type", "invoice_frequency", "start_date",
    "contract_term_years", "base_monthly_rent", "escalation_pct", "vat_treatment",
    "auto_renew", "notice_period_days",
    "status", "notes",
]


def insert_contract(data: dict) -> int:
    placeholders = ", ".join(["%s"] * len(CONTRACT_COLS))
    sql = f"INSERT INTO contracts ({', '.join(CONTRACT_COLS)}) VALUES ({placeholders}) RETURNING id"
    values = []
    for k in CONTRACT_COLS:
        v = data.get(k)
        if v is None and k == "auto_renew":
            v = 1
        if v is None and k == "notice_period_days":
            v = 60
        values.append(v)
    with conn() as c:
        cur = c.cursor()
        cur.execute(sql, values)
        return cur.fetchone()["id"]


def update_contract(contract_id: int, data: dict) -> None:
    set_clause = ", ".join(f"{col} = %s" for col in CONTRACT_COLS)
    sql = f"UPDATE contracts SET {set_clause}, updated_at = %s WHERE id = %s"
    params = [data.get(k) for k in CONTRACT_COLS] + [
        datetime.utcnow().isoformat(timespec="seconds"), contract_id,
    ]
    with conn() as c:
        cur = c.cursor()
        cur.execute(sql, params)


def delete_contract(contract_id: int) -> None:
    with conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM contracts WHERE id = %s", (contract_id,))


def resolve_flag(contract_id: int, flag_type: str, flag_month: str, by: str | None = None) -> None:
    with conn() as c:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO flag_actions (contract_id, flag_type, flag_month, resolved_by)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (contract_id, flag_type, flag_month) DO NOTHING""",
            (contract_id, flag_type, flag_month, by),
        )


def unresolve_flag(contract_id: int, flag_type: str, flag_month: str) -> None:
    with conn() as c:
        cur = c.cursor()
        cur.execute(
            "DELETE FROM flag_actions WHERE contract_id = %s AND flag_type = %s AND flag_month = %s",
            (contract_id, flag_type, flag_month),
        )


def resolved_flag_keys_for_month(flag_month: str) -> set[tuple[int, str]]:
    rows = fetchall(
        "SELECT contract_id, flag_type FROM flag_actions WHERE flag_month = %s",
        (flag_month,),
    )
    return {(r["contract_id"], r["flag_type"]) for r in rows}


def resolved_flags_for_month(flag_month: str) -> list[dict]:
    rows = fetchall(
        """SELECT f.*, c.client_name, c.project_number, c.sheet_source
           FROM flag_actions f
           JOIN contracts c ON c.id = f.contract_id
           WHERE f.flag_month = %s
           ORDER BY f.resolved_at DESC""",
        (flag_month,),
    )
    return [dict(r) for r in rows]


def insert_rent_override(contract_id: int, effective_date: str,
                         override_rent: float, reason: str | None = None) -> int:
    with conn() as c:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO rent_overrides (contract_id, effective_date, override_rent, reason)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (contract_id, effective_date)
               DO UPDATE SET override_rent = EXCLUDED.override_rent, reason = EXCLUDED.reason
               RETURNING id""",
            (contract_id, effective_date, override_rent, reason),
        )
        return cur.fetchone()["id"]


def delete_rent_override(override_id: int) -> None:
    with conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM rent_overrides WHERE id = %s", (override_id,))


def rent_overrides_for_contract(contract_id: int) -> list[dict]:
    rows = fetchall(
        "SELECT * FROM rent_overrides WHERE contract_id = %s ORDER BY effective_date ASC",
        (contract_id,),
    )
    return [dict(r) for r in rows]


def all_rent_overrides() -> dict[int, list[dict]]:
    rows = fetchall(
        "SELECT * FROM rent_overrides ORDER BY contract_id, effective_date ASC"
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["contract_id"], []).append(dict(r))
    return out
