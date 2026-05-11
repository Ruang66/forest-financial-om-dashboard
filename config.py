"""App config. Override via environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Source Excel file used by the initial migration
EXCEL_SOURCE = os.environ.get(
    "OM_EXCEL_SOURCE",
    str(BASE_DIR.parent / "O&M Sites.xlsx"),
)

# Company VAT rate for Residential Ex-VAT conversion in aggregated views
VAT_RATE = float(os.environ.get("OM_VAT_RATE", "0.15"))

# Financial year starts in March (SA convention, 1 March to end Feb).
FY_START_MONTH = int(os.environ.get("OM_FY_START_MONTH", "3"))
