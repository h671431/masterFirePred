# Master Fire Prediction

This repository contains code developed as part of my master's thesis in Applied datatechnology and engineering at Western Norway University of Applied Sciences.

The project investigates the relationship between fire incidents in BRIS (DSB) and weather data from MET Norway's Frost API using spatiotemporal analysis.

## Current status

- Project setup completed
- BRIS Excel exports are cleaned and stored in a local DuckDB database (`src/bris.py`)
- Station list and daily weather observations can be downloaded from Frost (`src/frost.py`)
- Data quality notebook (`notebooks/02_bris_datakvalitet.ipynb`)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
FROST_CLIENT_ID=<your Frost client id>
# Optional: folder with BRIS Excel exports (default: data/raw)
# BRIS_RAW_DIR=/path/to/excel
```

## Building the database

1. Put the BRIS Excel exports in `data/raw/` (or set `BRIS_RAW_DIR`).
2. Read and clean the BRIS data (takes ~2 minutes):
   ```bash
   python -m src.bris
   ```
3. Download weather stations and daily observations from Frost (takes a while; can be stopped and resumed):
   ```bash
   python -m src.frost --start 2016-01-01 --end 2026-11-01
   ```

4. Match incidents to weather (nearest core station, station-day panel, takes ~10 seconds):
   ```bash
   python -m src.matching
   ```

Everything is stored in `data/processed/master.duckdb`.

| Table | Content |
|---|---|
| `brann` | Fire incidents (building fires + fires spreading to buildings) |
| `falske` | Unnecessary and false call-outs |
| `kolonner` | Original BRIS column names → new names, and which were removed |
| `stasjoner` | MET weather stations with coordinates |
| `vaer_dogn_raw` | Daily weather values, long format |
| `vaer_dogn` | View: one row per station and day (temperature, precipitation, wind, humidity, snow depth), with quality filtering |
| `stasjon_kjerne` | Core stations with temperature on ≥ 80 % of days |
| `hendelse_stasjon` | Each incident → nearest core station and distance (km) |
| `panel_stasjon_dag` | One row per core station and day: weather, lags and incident counts per group |

## Data and privacy

The BRIS data contain information that can identify people (addresses, free text, risk factors).
Columns with addresses, free-text remarks and personal risk factors are **removed** when reading the data (see `SENSITIVE_PREFIXES` in `src/bris.py`).
The `data/` folder is in `.gitignore` and must never be committed.

## Project structure

- `src/config.py` - paths and settings
- `src/bris.py` - reading and cleaning BRIS exports
- `src/frost.py` - Frost API (stations and daily observations)
- `src/matching.py` - spatiotemporal matching of incidents and weather
- `src/db.py` - DuckDB helpers
- `figs/` - aggregated figures (no personal data)
- `notebooks/` - exploratory notebooks
- `data/` - local data files (not committed)
