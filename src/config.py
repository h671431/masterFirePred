import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

FROST_CLIENT_ID = os.getenv("FROST_CLIENT_ID")

DATA_DIR = PROJECT_ROOT / "data"

# Mappe med BRIS-uttrekkene (Excel). Kan overstyres med BRIS_RAW_DIR i .env.
RAW_DIR = Path(os.getenv("BRIS_RAW_DIR", DATA_DIR / "raw"))

PROCESSED_DIR = DATA_DIR / "processed"

# DuckDB-databasen som alt lagres i. Ligger under data/ og committes aldri.
DB_PATH = Path(os.getenv("DB_PATH", PROCESSED_DIR / "master.duckdb"))
