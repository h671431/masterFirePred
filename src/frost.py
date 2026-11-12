"""Henting av værdata fra MET Norway sitt Frost API (https://frost.met.no).

Last ned stasjoner og døgnverdier til DuckDB:

    python -m src.frost --start 2016-01-01 --end 2026-11-01

Tabeller:
    stasjoner      - alle norske stasjoner (SensorSystem) med koordinater
    vaer_dogn_raw  - døgnverdier i langt format (én rad per stasjon/dag/element/timeoffset)
    vaer_dogn      - (view) bredt format med én valgt timeoffset per element
    frost_progress - hvilke batcher som er ferdige, slik at nedlastingen kan gjenopptas

Merk: Frost sine døgnverdier er definert i UTC. Nedbør (P1D) er standard målt
06-06 UTC (timeoffset PT6H) og lagres på datoen perioden starter. BRIS-tid er lokal norsk tid.
"""

from __future__ import annotations

import argparse
import time

import pandas as pd
import requests

from src.config import FROST_CLIENT_ID
from src import db

BASE_URL = "https://frost.met.no"
FROST_OBSERVATIONS_URL = f"{BASE_URL}/observations/v0.jsonld"

# Element-ID i Frost -> kolonnenavn, og hvilken timeoffset vi foretrekker.
DAILY_ELEMENTS = {
    "mean(air_temperature P1D)": ("temp_mean", "PT0H"),
    "min(air_temperature P1D)": ("temp_min", "PT0H"),
    "max(air_temperature P1D)": ("temp_max", "PT0H"),
    "sum(precipitation_amount P1D)": ("nedbor", "PT6H"),
    "mean(wind_speed P1D)": ("vind_mean", "PT0H"),
    "mean(relative_humidity P1D)": ("rh_mean", "PT0H"),
    "surface_snow_thickness": ("snodybde", "PT6H"),
}


def _get(path_or_url: str, params: dict, retries: int = 5) -> dict | None:
    """GET mot Frost. Returnerer None når Frost svarer 404/412 (ingen data)."""
    if not FROST_CLIENT_ID:
        raise RuntimeError("FROST_CLIENT_ID mangler i .env")
    url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
    last_status = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, auth=(FROST_CLIENT_ID, ""), timeout=120)
        except requests.RequestException:
            time.sleep(2**attempt)
            continue
        if r.status_code in (404, 412):
            return None
        # 403 kan komme midlertidig ved mange forespørsler etter hverandre
        if r.status_code in (403, 429, 500, 502, 503, 504):
            last_status = r.status_code
            time.sleep(2 ** (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise FrostError(f"Frost svarte ikke etter {retries} forsøk (status {last_status}): {params.get('sources')}")


class FrostError(RuntimeError):
    pass


# --- Opprinnelige funksjoner (brukes i notebooks/01_explore_frost.ipynb) ---


def fetch_observations(source: str, elements: list[str], start_date: str, end_date: str):
    return _get(
        FROST_OBSERVATIONS_URL,
        {"sources": source, "elements": ",".join(elements), "referencetime": f"{start_date}/{end_date}"},
    )


def parse_hourly_observations(data):
    rows = []
    for item in data["data"]:
        row = {
            "timestamp": item["referenceTime"],
            "air_temperature": None,
            "relative_humidity": None,
            "wind_speed": None,
        }
        for obs in item["observations"]:
            element = obs["elementId"]
            level = obs.get("level", {}).get("value")
            if obs.get("timeResolution") != "PT1H":
                continue
            if element == "air_temperature" and level == 2:
                row["air_temperature"] = obs["value"]
            elif element == "relative_humidity" and level == 2:
                row["relative_humidity"] = obs["value"]
            elif element == "wind_speed" and level == 10:
                row["wind_speed"] = obs["value"]
        if any(row[k] is not None for k in ["air_temperature", "relative_humidity", "wind_speed"]):
            rows.append(row)
    return rows


# --- Stasjoner ---


def fetch_stations() -> pd.DataFrame:
    data = _get("/sources/v0.jsonld", {"types": "SensorSystem", "country": "NO"})
    rows = []
    for s in data["data"]:
        coords = (s.get("geometry") or {}).get("coordinates") or [None, None]
        rows.append(
            {
                "stasjon_id": s["id"],
                "navn": s.get("name"),
                "lon": coords[0],
                "lat": coords[1],
                "moh": s.get("masl"),
                "gyldig_fra": pd.to_datetime(s.get("validFrom"), utc=True, errors="coerce"),
                "gyldig_til": pd.to_datetime(s.get("validTo"), utc=True, errors="coerce"),
                "kommunenr": str(s["municipalityId"]).zfill(4) if s.get("municipalityId") else None,
                "kommune": s.get("municipality"),
                "fylke": s.get("county"),
            }
        )
    return pd.DataFrame(rows)


def stations_with_data(elements: list[str], start: str, end: str) -> set[str]:
    """Stasjoner som har minst ett av elementene i perioden."""
    ids: set[str] = set()
    for el in elements:
        data = _get(
            "/observations/availableTimeSeries/v0.jsonld",
            {"elements": el, "referencetime": f"{start}/{end}"},
        )
        for ts in (data or {}).get("data", []):
            ids.add(ts["sourceId"].split(":")[0])
    return ids


# --- Døgnverdier ---


def fetch_daily(sources: list[str], start: str, end: str, elements: list[str] | None = None) -> pd.DataFrame:
    elements = elements or list(DAILY_ELEMENTS)
    data = _get(
        FROST_OBSERVATIONS_URL,
        {
            "sources": ",".join(sources),
            "elements": ",".join(elements),
            "referencetime": f"{start}/{end}",
            "timeoffsets": "PT0H,PT6H",
            "levels": "default",
            "qualities": "0,1,2,3,4",
        },
    )
    rows = []
    for item in (data or {}).get("data", []):
        sid = item["sourceId"].split(":")[0]
        dato = item["referenceTime"][:10]
        for obs in item["observations"]:
            rows.append(
                {
                    "stasjon_id": sid,
                    "dato": dato,
                    "element": obs["elementId"],
                    "timeoffset": obs.get("timeOffset"),
                    "verdi": obs.get("value"),
                    "kvalitet": obs.get("qualityCode"),
                }
            )
    df = pd.DataFrame(rows, columns=["stasjon_id", "dato", "element", "timeoffset", "verdi", "kvalitet"])
    df["dato"] = pd.to_datetime(df["dato"]).dt.date
    return df


# Fysisk rimelige grenser per kolonne for Norge (varmerekord 35,6 °C, kulderekord -51,4 °C).
# Verdier utenfor tas ikke med i vaer_dogn.
PLAUSIBLE = {
    "temp_mean": (-50, 30),
    "temp_min": (-55, 28),
    "temp_max": (-50, 36),
    "nedbor": (0, 300),
    "vind_mean": (0, 50),
    "rh_mean": (0, 100),
    "snodybde": (0, 1000),
}

# MET sine kvalitetskoder: 0-2 regnes som brukbare, 3 og høyere utelates.
MAX_QUALITY = 2


def _create_wide_view(con) -> None:
    """Bredt format med kvalitetskontroll.

    - Flere tidsserier for samme stasjon/dag/element (vanlig for snødybde) slås sammen med gjennomsnitt.
    - Verdier med kvalitetskode over MAX_QUALITY utelates.
    - Verdier utenfor PLAUSIBLE utelates.
    vaer_dogn_raw beholder alle verdier uendret.
    """
    cases = []
    for el, (col, off) in DAILY_ELEMENTS.items():
        lo, hi = PLAUSIBLE[col]
        cases.append(
            f"avg(verdi) FILTER (WHERE element = '{el}' AND timeoffset = '{off}' "
            f"AND (kvalitet IS NULL OR kvalitet <= {MAX_QUALITY}) "
            f"AND verdi BETWEEN {lo} AND {hi}) AS {col}"
        )
    con.execute(
        "CREATE OR REPLACE VIEW vaer_dogn AS\nSELECT stasjon_id, dato,\n  "
        + ",\n  ".join(cases)
        + "\nFROM vaer_dogn_raw\nGROUP BY stasjon_id, dato"
    )


def download(start: str, end: str, batch_size: int = 10) -> None:
    con = db.connect()
    con.execute("CREATE TABLE IF NOT EXISTS frost_progress (batch VARCHAR PRIMARY KEY, rader BIGINT)")

    print("Henter stasjonsliste ...", flush=True)
    stations = fetch_stations()
    db.write_table(stations, "stasjoner", con=con)

    print("Finner stasjoner med relevante data ...", flush=True)
    ids = sorted(stations_with_data(list(DAILY_ELEMENTS), start, end) & set(stations["stasjon_id"]))
    print(f"  {len(ids)} stasjoner", flush=True)

    done = {r[0] for r in con.execute("SELECT batch FROM frost_progress").fetchall()}
    years = range(int(start[:4]), int(end[:4]) + 1)
    batches = [ids[i : i + batch_size] for i in range(0, len(ids), batch_size)]
    total = len(batches) * len(years)
    n = 0
    for year in years:
        y_start = max(start, f"{year}-01-01")
        y_end = min(end, f"{year + 1}-01-01")
        if y_start >= y_end:
            continue
        for b in batches:
            n += 1
            key = f"{year}:{b[0]}-{b[-1]}"
            if key in done:
                continue
            try:
                df = fetch_daily(b, y_start, y_end)
            except FrostError:
                # Del opp batchen og hopp over stasjoner som fortsatt feiler
                parts = []
                for sid in b:
                    try:
                        parts.append(fetch_daily([sid], y_start, y_end))
                    except FrostError as e:
                        print(f"  hopper over {sid} {year}: {e}", flush=True)
                df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
            if len(df):
                db.write_table(df, "vaer_dogn_raw", con=con, replace=False)
            con.execute("INSERT INTO frost_progress VALUES (?, ?)", [key, len(df)])
            print(f"  [{n}/{total}] {key}: {len(df):,} verdier", flush=True)

    _create_wide_view(con)
    con.close()
    print("Ferdig.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Last ned døgnverdier fra Frost til DuckDB")
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--batch-size", type=int, default=10)
    a = p.parse_args()
    download(a.start, a.end, a.batch_size)
