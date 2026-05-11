# Forest Energy O&M Dashboard

A FastAPI + SQLite web app for tracking O&M contracts, escalations, and forward revenue. Replaces manual upkeep of `O&M Sites.xlsx`.

## What it does

- Dashboard view: total sites under management, monthly and annual value, current financial year outlook
- Current-month flags: red for contracts escalating or ending this month (action required), orange for the month ahead
- 12-month rolling forward revenue roll-up per contract, Ex VAT equivalent totals (Residential converted at 15%)
- CRUD for contracts: add, edit, delete
- Initial import from your existing `O&M Sites.xlsx`, with data quality issues flagged as `incomplete`

## Quick start (Windows)

1. Install Python 3.10 or newer from python.org (tick "Add python.exe to PATH" during install).
2. Open PowerShell in this folder (`om_dashboard`).
3. Create a virtual environment and install dependencies:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
4. Run the one-off Excel import (the default path points to `..\O&M Sites.xlsx`):
   ```powershell
   python migrate_excel.py
   ```
5. Start the app:
   ```powershell
   uvicorn app:app --reload --port 8000
   ```
6. Open http://127.0.0.1:8000 in your browser.

## Files

- `app.py` - FastAPI routes (dashboard, contract list, CRUD)
- `dashboard_service.py` - aggregation logic (KPIs, flags, 12-month roll-up, FY forecast)
- `contract_math.py` - per-contract math: escalations, next escalation date, end date, rent per month
- `db.py` - SQLite access layer
- `migrate_excel.py` - one-off import from `O&M Sites.xlsx`
- `config.py` - paths, VAT rate, financial year start
- `templates/` - Jinja2 HTML templates
- `static/styles.css` - Forest Energy styling
- `om_dashboard.db` - SQLite database (created on first run)

## Data quality flags

The migration marks any contract missing start date, invoice type, base rent, term, or escalation as `incomplete`. Incomplete contracts are excluded from all billing totals and surfaced on the dashboard for you to fix in the contract editor. The Forest Energy owned sites (Herfsvreudge, Jon Stoffberg, Bay Village, New National, Marble Hall) come in with status `internal_no_invoice` and are also excluded from billing totals.

## Re-running the import

Running `python migrate_excel.py` wipes the contracts table and reloads from Excel. Any edits you made in the dashboard after the first import will be lost. Once you have adopted the dashboard as the source of truth, do not re-run the migration.

To point the migration at a different Excel file:

```powershell
python migrate_excel.py "C:\path\to\your\file.xlsx"
```

## Financial year

The dashboard assumes a 1 March to end-February financial year (SA convention). Override via the `OM_FY_START_MONTH` environment variable if that changes.

## VAT handling

- Commercial contracts: values stored and displayed Ex VAT.
- Residential PPA contracts: values stored and displayed Inc VAT (as you keep them today).
- For comparable totals (e.g. "Total Monthly Value", FY Forecast, 12-month forward), Residential is automatically converted to Ex VAT equivalent at 15%.

## Next phases

Phase 2 (not built yet) will add Outlook integration via Microsoft Graph so an agent can draft escalation notification emails from the Client Email field on each contract. That field is already in the schema.

Phase 3: cloud deployment. The app is deployment-ready for Render, Railway, or Azure App Service. Add auth before going public.

## Adding auth later

Auth was left out for the first version. For a public link with a shared password, add a `BasicAuthMiddleware` that checks `OM_PASSWORD` from the environment against HTTP Basic credentials. For per-user logins (you, Finance, O&M Manager), add a `users` table and a simple session-cookie login page.
