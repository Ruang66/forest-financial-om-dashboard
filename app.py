"""FastAPI entry point for the O&M Dashboard."""
import os
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import db
from dashboard_service import build_home, build_forecast, build_category

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Forest Energy O&M Dashboard")

SESSION_SECRET = os.environ.get("SESSION_SECRET", "change-me-in-production")
OM_USERNAME = os.environ.get("OM_USERNAME", "Forest")
OM_PASSWORD = os.environ.get("OM_PASSWORD", "")

_PUBLIC_PATHS = {"/login", "/static"}

class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path == "/login":
            return await call_next(request)
        if not request.session.get("logged_in"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

# Order matters: _AuthMiddleware first so SessionMiddleware wraps it (runs first)
app.add_middleware(_AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---- Jinja filters ----------------------------------------------------------

def _zar(value) -> str:
    if value is None or value == "":
        return "R 0"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "R 0"
    rounded = int(round(v))
    formatted = f"{rounded:,}".replace(",", " ")
    return f"R {formatted}"


def _pct(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_date(value) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (date, datetime)):
        return value.strftime("%d %b %Y")
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(value)


def _month_label(value) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%b %y")
    return str(value)


templates.env.filters["zar"] = _zar
templates.env.filters["pct"] = _pct
templates.env.filters["fmt_date"] = _fmt_date
templates.env.filters["month_label"] = _month_label


# ---- Startup ---------------------------------------------------------------

@app.on_event("startup")
def _startup():
    db.init_db()


# ---- Auth routes -----------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if request.session.get("logged_in"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == OM_USERNAME and password == OM_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Incorrect username or password."},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- Routes ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    data = build_home()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "data": data},
    )


@app.get("/forecast", response_class=HTMLResponse)
def forecast(request: Request):
    data = build_forecast()
    return templates.TemplateResponse(
        "forecast.html",
        {"request": request, "data": data},
    )


@app.get("/commercial", response_class=HTMLResponse)
def commercial(request: Request):
    data = build_category("commercial")
    return templates.TemplateResponse(
        "category.html",
        {"request": request, "data": data},
    )


@app.get("/residential", response_class=HTMLResponse)
def residential(request: Request):
    data = build_category("residential")
    return templates.TemplateResponse(
        "category.html",
        {"request": request, "data": data},
    )


# ---- Flag actions ----------------------------------------------------------

@app.post("/flags/resolve")
def flags_resolve(
    request: Request,
    contract_id: int = Form(...),
    flag_type: str = Form(...),
    flag_month: str = Form(...),
    redirect: str = Form("/"),
):
    db.resolve_flag(contract_id, flag_type, flag_month)
    return RedirectResponse(redirect, status_code=303)


@app.post("/flags/unresolve")
def flags_unresolve(
    request: Request,
    contract_id: int = Form(...),
    flag_type: str = Form(...),
    flag_month: str = Form(...),
    redirect: str = Form("/"),
):
    db.unresolve_flag(contract_id, flag_type, flag_month)
    return RedirectResponse(redirect, status_code=303)


@app.get("/contracts", response_class=HTMLResponse)
def contracts_list(request: Request, status: str | None = None, source: str | None = None):
    sql = "SELECT * FROM contracts WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    if source:
        sql += " AND sheet_source = %s"
        params.append(source)
    sql += " ORDER BY sheet_source, client_name"
    rows = [dict(r) for r in db.fetchall(sql, params)]
    return templates.TemplateResponse(
        "contracts_list.html",
        {
            "request": request,
            "contracts": rows,
            "filter_status": status,
            "filter_source": source,
        },
    )


@app.get("/contracts/new", response_class=HTMLResponse)
def contracts_new(request: Request):
    return templates.TemplateResponse(
        "contract_form.html",
        {"request": request, "contract": None, "mode": "new"},
    )


def _parse_contract_form(
    sheet_source, project_number, client_name, contact_person, client_email,
    invoice_type, invoice_frequency, start_date, contract_term_years, base_monthly_rent,
    escalation_pct, vat_treatment, auto_renew, notice_period_days, status, notes,
) -> dict:
    return {
        "sheet_source": sheet_source,
        "project_number": project_number or None,
        "client_name": client_name.strip(),
        "contact_person": contact_person or None,
        "client_email": client_email or None,
        "invoice_type": invoice_type or None,
        "invoice_frequency": invoice_frequency or "monthly",
        "start_date": start_date or None,
        "contract_term_years": float(contract_term_years) if contract_term_years else None,
        "base_monthly_rent": float(base_monthly_rent) if base_monthly_rent else None,
        "escalation_pct": float(escalation_pct) / 100 if escalation_pct else None,
        "vat_treatment": vat_treatment,
        "auto_renew": 1 if str(auto_renew or "").lower() in ("1", "true", "on", "yes") else 0,
        "notice_period_days": int(notice_period_days) if notice_period_days else 60,
        "status": status,
        "notes": notes or None,
    }


@app.post("/contracts/new")
def contracts_new_post(
    sheet_source: str = Form(...),
    project_number: str | None = Form(None),
    client_name: str = Form(...),
    contact_person: str | None = Form(None),
    client_email: str | None = Form(None),
    invoice_type: str | None = Form(None),
    invoice_frequency: str = Form("monthly"),
    start_date: str | None = Form(None),
    contract_term_years: str | None = Form(None),
    base_monthly_rent: str | None = Form(None),
    escalation_pct: str | None = Form(None),
    vat_treatment: str = Form(...),
    auto_renew: str | None = Form(None),
    notice_period_days: str | None = Form(None),
    status: str = Form("active"),
    notes: str | None = Form(None),
):
    data = _parse_contract_form(
        sheet_source, project_number, client_name, contact_person, client_email,
        invoice_type, invoice_frequency, start_date, contract_term_years, base_monthly_rent,
        escalation_pct, vat_treatment, auto_renew, notice_period_days, status, notes,
    )
    new_id = db.insert_contract(data)
    return RedirectResponse(f"/contracts/{new_id}", status_code=303)


@app.get("/contracts/{contract_id}", response_class=HTMLResponse)
def contract_detail(request: Request, contract_id: int):
    row = db.fetchone("SELECT * FROM contracts WHERE id = %s", (contract_id,))
    if not row:
        raise HTTPException(404, "Contract not found")
    contract = dict(row)
    overrides = db.rent_overrides_for_contract(contract_id)
    from contract_math import compute_contract
    contract["computed"] = compute_contract(contract, overrides=overrides)
    return templates.TemplateResponse(
        "contract_form.html",
        {
            "request": request,
            "contract": contract,
            "mode": "edit",
            "overrides": overrides,
        },
    )


@app.post("/contracts/{contract_id}")
def contract_update(
    contract_id: int,
    sheet_source: str = Form(...),
    project_number: str | None = Form(None),
    client_name: str = Form(...),
    contact_person: str | None = Form(None),
    client_email: str | None = Form(None),
    invoice_type: str | None = Form(None),
    invoice_frequency: str = Form("monthly"),
    start_date: str | None = Form(None),
    contract_term_years: str | None = Form(None),
    base_monthly_rent: str | None = Form(None),
    escalation_pct: str | None = Form(None),
    vat_treatment: str = Form(...),
    auto_renew: str | None = Form(None),
    notice_period_days: str | None = Form(None),
    status: str = Form("active"),
    notes: str | None = Form(None),
):
    data = _parse_contract_form(
        sheet_source, project_number, client_name, contact_person, client_email,
        invoice_type, invoice_frequency, start_date, contract_term_years, base_monthly_rent,
        escalation_pct, vat_treatment, auto_renew, notice_period_days, status, notes,
    )
    db.update_contract(contract_id, data)
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


@app.post("/contracts/{contract_id}/delete")
def contract_delete(contract_id: int):
    db.delete_contract(contract_id)
    return RedirectResponse("/contracts", status_code=303)


# ---- Rent overrides --------------------------------------------------------

@app.post("/contracts/{contract_id}/overrides")
def add_override(
    contract_id: int,
    effective_date: str = Form(...),
    override_rent: float = Form(...),
    reason: str | None = Form(None),
):
    db.insert_rent_override(contract_id, effective_date, override_rent, reason or None)
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


@app.post("/contracts/{contract_id}/overrides/{override_id}/edit")
def edit_override(
    contract_id: int,
    override_id: int,
    effective_date: str = Form(...),
    override_rent: float = Form(...),
    reason: str | None = Form(None),
):
    db.insert_rent_override(contract_id, effective_date, override_rent, reason or None)
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


@app.post("/contracts/{contract_id}/overrides/{override_id}/delete")
def delete_override(contract_id: int, override_id: int):
    db.delete_rent_override(override_id)
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)
