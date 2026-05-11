@echo off
REM Quick-start script for Windows.

if not exist .venv\ (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM Always ensure dependencies are present (no-op if already installed).
pip install -q -r requirements.txt

if not exist om_dashboard.db (
    echo First run: importing from Excel...
    python migrate_excel.py
)

echo.
echo Starting Forest Energy O^&M Dashboard at http://127.0.0.1:8000
echo Press Ctrl+C to stop.
echo.

REM Open the dashboard in the default browser after a short delay so uvicorn has time to bind.
start "" /B cmd /C "timeout /t 3 /nobreak > nul && start http://127.0.0.1:8000"

uvicorn app:app --reload --port 8000