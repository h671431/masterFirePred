"""Kobling av BRIS-hendelser og værdata (RQ1).

    python -m src.matching

Metode (første versjon):
1. Kjernestasjoner: stasjoner med døgntemperatur for minst MIN_COVERAGE av dagene i perioden.
2. Hver hendelse (brann og falske utrykninger) kobles til nærmeste kjernestasjon (haversine-avstand).
   Det gir et nedslagsfelt per stasjon (Voronoi-celler), dvs. punktbasert romlig oppløsning.
3. Stasjon-dag-panel: én rad per kjernestasjon og dag med vær og antall hendelser per gruppe
   (0 når det ikke var hendelser). Dette er grunnlaget for rater, lag-analyse og ML.

Merk: BRIS-dato er lokal norsk tid, mens Frost sine døgnverdier for temperatur gjelder 00-00 UTC.
Forskjellen er 1-2 timer og ignoreres i denne versjonen.

Tabeller:
    stasjon_kjerne      - kjernestasjonene
    hendelse_stasjon    - hendelse -> nærmeste kjernestasjon + avstand (km)
    panel_stasjon_dag   - stasjon x dag med vær, lag og antall hendelser
"""

from __future__ import annotations

from src import db

START = "2016-01-01"
END = "2026-10-31"
MIN_COVERAGE = 0.8

# Grupper som telles i panelet: kolonnenavn -> SQL-betingelse på hendelsene
GROUPS = {
    "n_bygning": "kilde = 'bygg' AND oppdragstype = 'Brann i bygning'",
    "n_skorstein": "kilde = 'bygg' AND oppdragstype = 'Brann i skorstein'",
    "n_komfyr": "kilde = 'bygg' AND oppdragstype = 'Brannhindrende tiltak komfyr'",
    "n_brannhindrende_annet": "kilde = 'bygg' AND oppdragstype = 'Brannhindrende annet i bygg'",
    "n_spredning": "kilde = 'spredning_til_bygg'",
    "n_falske": "kilde = 'falske'",
}

HAVERSINE = (
    "2 * 6371 * asin(sqrt(pow(sin(radians(s.lat - h.lat) / 2), 2) + "
    "cos(radians(h.lat)) * cos(radians(s.lat)) * pow(sin(radians(s.lon - h.lon) / 2), 2)))"
)


def build(con=None) -> None:
    own = con is None
    con = con or db.connect()
    n_days = con.execute(f"SELECT date_diff('day', DATE '{START}', DATE '{END}') + 1").fetchone()[0]

    print("Velger kjernestasjoner ...", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TABLE stasjon_kjerne AS
        SELECT s.stasjon_id, s.navn, s.lat, s.lon, s.moh, s.kommunenr, s.fylke,
               count(v.temp_mean) AS dager_med_temp,
               count(v.temp_mean) / {n_days} AS dekning
        FROM stasjoner s
        JOIN vaer_dogn v USING (stasjon_id)
        WHERE v.dato BETWEEN DATE '{START}' AND DATE '{END}'
          AND s.lat BETWEEN 57.5 AND 72 AND s.lon BETWEEN 4 AND 32   -- fastlandet
        GROUP BY ALL
        HAVING count(v.temp_mean) >= {MIN_COVERAGE} * {n_days}
    """)
    n_st = con.execute("SELECT count(*) FROM stasjon_kjerne").fetchone()[0]
    print(f"  {n_st} kjernestasjoner", flush=True)

    print("Kobler hendelser til nærmeste stasjon ...", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TABLE hendelse_stasjon AS
        WITH h AS (
            SELECT oppdrag_id, kilde, oppdragstype, dato, lat, lon FROM brann
            WHERE lat BETWEEN 57.5 AND 72 AND lon BETWEEN 4 AND 32
            UNION ALL
            SELECT oppdrag_id, kilde, oppdragstype, dato, lat, lon FROM falske
            WHERE lat BETWEEN 57.5 AND 72 AND lon BETWEEN 4 AND 32
        )
        SELECT h.oppdrag_id, h.kilde, h.oppdragstype, h.dato,
               arg_min(s.stasjon_id, {HAVERSINE}) AS stasjon_id,
               min({HAVERSINE}) AS avstand_km
        FROM h CROSS JOIN stasjon_kjerne s
        GROUP BY h.oppdrag_id, h.kilde, h.oppdragstype, h.dato
    """)
    n_h = con.execute("SELECT count(*) FROM hendelse_stasjon").fetchone()[0]
    print(f"  {n_h:,} hendelser koblet", flush=True)

    print("Bygger stasjon-dag-panel ...", flush=True)
    counts = ",\n".join(
        f"count(*) FILTER (WHERE {cond}) AS {col}" for col, cond in GROUPS.items()
    )
    zeros = ",\n".join(f"coalesce(c.{col}, 0) AS {col}" for col in GROUPS)
    con.execute(f"""
        CREATE OR REPLACE TABLE panel_stasjon_dag AS
        WITH c AS (
            SELECT stasjon_id, dato, {counts}
            FROM hendelse_stasjon
            GROUP BY stasjon_id, dato
        ),
        v AS (
            SELECT v.*,
                   lag(temp_mean, 1) OVER w AS temp_mean_lag1,
                   lag(temp_mean, 2) OVER w AS temp_mean_lag2,
                   lag(temp_mean, 3) OVER w AS temp_mean_lag3,
                   avg(temp_mean) OVER (w ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS temp_mean_7d,
                   sum(nedbor) OVER (w ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS nedbor_7d
            FROM vaer_dogn v
            JOIN stasjon_kjerne k USING (stasjon_id)
            WHERE v.dato BETWEEN DATE '{START}' AND DATE '{END}'
            WINDOW w AS (PARTITION BY v.stasjon_id ORDER BY v.dato)
        )
        SELECT v.stasjon_id, v.dato, year(v.dato) AS aar, month(v.dato) AS mnd,
               dayofweek(v.dato) AS ukedag,
               v.temp_mean, v.temp_min, v.temp_max, v.nedbor, v.vind_mean, v.rh_mean, v.snodybde,
               v.temp_mean_lag1, v.temp_mean_lag2, v.temp_mean_lag3, v.temp_mean_7d, v.nedbor_7d,
               {zeros}
        FROM v LEFT JOIN c USING (stasjon_id, dato)
    """)
    n_p = con.execute("SELECT count(*) FROM panel_stasjon_dag").fetchone()[0]
    print(f"  {n_p:,} stasjonsdager", flush=True)
    if own:
        con.close()
    print("Ferdig.")


if __name__ == "__main__":
    build()
