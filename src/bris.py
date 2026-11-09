"""Innlesing og rensing av BRIS-uttrekk (Excel fra brannstatistikk.no).

Kjør fra prosjektroten for å bygge databasen:

    python -m src.bris

Resultat i DuckDB (data/processed/master.duckdb):
    brann   - alle brannhendelser (byggbranner + branner med spredning til bygg)
    falske  - unødige og falske utrykninger
    kolonner - oversikt over originale kolonnenavn -> nye navn, og hva som er fjernet

Sensitive kolonner (adresser, fritekst, risikofaktorer om personer) tas IKKE med
i databasen, se SENSITIVE_PREFIXES.
"""

from __future__ import annotations

import re
import sys
import unicodedata
import warnings
from pathlib import Path

import openpyxl
import pandas as pd

from src.config import RAW_DIR
from src import db

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# Faste, korte navn på de viktigste kolonnene.
CORE_COLUMNS = {
    "Oppdrag ID": "oppdrag_id",
    "110 ID": "id_110",
    "Opprinnelig oppdragstype": "opprinnelig_oppdragstype",
    "Oppdragstype": "oppdragstype",
    "Overordnet oppdragstype": "overordnet_oppdragstype",
    "Kategori oppdrag": "kategori",
    "Postnr": "postnr",
    "Poststed": "poststed",
    "Kommunenavn": "kommunenavn",
    "Kommunenr": "kommunenr",
    "Kommunenr/-navn": "kommune",
    "Fylke": "fylke",
    "Geolokasjon breddegrad": "lat",
    "Geolokasjon lengdegrad": "lon",
    "Ansvarlig brannvesen i hendelseskommune": "ansvarlig_brannvesen",
    "Utvarslede brannvesen": "utvarslede_brannvesen",
    "110-sentral": "sentral_110",
    "Dato anrop": "dato_anrop",
    "Tid anrop": "tid_anrop",
    "Stasjoner": "stasjoner",
    "Adresse funnet": "adresse_funnet",
    "Innsatsvarighet": "innsatsvarighet",
    "Type bolig": "type_bolig",
    "Antatt arnested": "antatt_arnested",
    "Antatt tennkilde": "antatt_tennkilde",
    "Brann- og redningsvesenets vurdering av hvordan brannen kan ha startet": "brv_vurdering_start",
    "Antar du at brannen er påsatt?": "antatt_pasatt",
    "Arnested - politi": "politi_arnested",
    "Brannen startet i - politi": "politi_startet_i",
    "Brannårsak overordnet - politi": "politi_brannarsak",
    "Hendelsestedets bygningstype": "bygningstype",
    "Hendelsestedets bygningstypekode": "bygningstypekode",
    "Avstand i meter til nærmeste bolig eller næringsbygg brannen spredte seg til": "spredning_avstand_m",
}

# Kolonner som kan identifisere personer eller inneholder helseopplysninger.
# Matches på starten av originalt kolonnenavn.
SENSITIVE_PREFIXES = (
    "Oppdragsadresse",
    "Adresseobjekt",
    "Merknader",
    "Adresse for bygget",
    "Hendelsesstedets bygningsnr",
    "Hendelsesstedets bruksenhetsnr",
    "Risikofaktorer",
)

# Filnavn -> kilde. Filer som ikke matcher noe hoppes over.
FILE_SOURCES = [
    (re.compile(r"^branner \d{4}-\d{4}", re.I), "brann", "bygg"),
    (re.compile(r"^branner med spredning", re.I), "brann", "spredning_til_bygg"),
    (re.compile(r"^un.dige og falske", re.I), "falske", "falske"),
]


def slugify(text: str, maxlen: int = 60) -> str:
    text = text.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:maxlen].rstrip("_")


def is_sensitive(original_name: str) -> bool:
    return original_name.startswith(SENSITIVE_PREFIXES)


def read_bris_excel(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Les et BRIS-uttrekk. Finner selv raden med kolonnenavn (starter med 'Oppdrag ID')."""
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = None
    for row in rows:
        if row and row[0] == "Oppdrag ID":
            header = [str(h).strip() if h is not None else "" for h in row]
            break
    if header is None:
        raise ValueError(f"Fant ikke kolonnerad ('Oppdrag ID') i {path.name}")
    data = [r for r in rows if r and r[0] is not None]
    wb.close()
    return pd.DataFrame(data, columns=range(len(header))), header


def build_column_names(header: list[str]) -> list[str]:
    names, seen = [], {}
    for h in header:
        first_line = h.split("\n")[0]
        if h in CORE_COLUMNS:
            name = CORE_COLUMNS[h]
        elif " - " in first_line:
            # Flervalgskolonner: "Spørsmål - Alternativ" -> sporsmal__alternativ
            q, opt = first_line.rsplit(" - ", 1)
            name = f"{slugify(q, 28)}__{slugify(opt, 32)}"
        else:
            name = slugify(first_line) or "kolonne"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        names.append(name)
    return names


def _to_seconds(v):
    if v is None or v == "":
        return None
    if hasattr(v, "total_seconds"):
        return v.total_seconds()
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", str(v))
    if m:
        h, mi, s = map(int, m.groups())
        return h * 3600 + mi * 60 + s
    return None


def clean(df: pd.DataFrame, header: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    names = build_column_names(header)
    meta = pd.DataFrame(
        {"original": header, "kolonne": names, "fjernet_sensitiv": [is_sensitive(h) for h in header]}
    )
    df.columns = names
    df = df[[n for n, s in zip(names, meta["fjernet_sensitiv"]) if not s]].copy()

    # Tomme strenger -> manglende verdi
    df = df.replace({"": None})

    # Tidspunkt for anrop (lokal norsk tid, uten tidssone i kilden)
    df["tidspunkt"] = pd.to_datetime(
        df["dato_anrop"].astype("string") + " " + df["tid_anrop"].astype("string"),
        format="%d.%m.%Y %H:%M:%S",
        errors="coerce",
    )
    df["dato"] = df["tidspunkt"].dt.date
    df = df.drop(columns=["dato_anrop", "tid_anrop"])

    df["adresse_funnet"] = pd.to_datetime(df["adresse_funnet"], errors="coerce")
    df["innsatsvarighet_s"] = df["innsatsvarighet"].map(_to_seconds).astype("float64")
    df = df.drop(columns=["innsatsvarighet"])

    df["oppdrag_id"] = pd.to_numeric(df["oppdrag_id"], errors="coerce").astype("Int64")
    for c in ("lat", "lon"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["kommunenr"] = (
        pd.to_numeric(df["kommunenr"], errors="coerce").astype("Int64").astype("string").str.zfill(4)
    )
    df["postnr"] = pd.to_numeric(df["postnr"], errors="coerce").astype("Int64").astype("string").str.zfill(4)

    # Resten av kolonnene lagres som tekst for å unngå blandede typer
    fixed = {"tidspunkt", "dato", "adresse_funnet", "innsatsvarighet_s", "oppdrag_id", "lat", "lon"}
    for c in df.columns:
        if c not in fixed:
            df[c] = df[c].map(lambda v: None if v is None else str(v)).astype("string")

    df = df.dropna(subset=["oppdrag_id"])
    return df, meta


def load_all(raw_dir: Path = RAW_DIR) -> dict[str, pd.DataFrame]:
    tables: dict[str, list[pd.DataFrame]] = {}
    metas = []
    files = sorted(raw_dir.glob("*.xlsx"))
    if not files:
        raise FileNotFoundError(f"Ingen .xlsx-filer i {raw_dir}")
    for path in files:
        match = next(((t, k) for rx, t, k in FILE_SOURCES if rx.search(path.name)), None)
        if match is None:
            print(f"  hopper over {path.name} (ukjent filtype)")
            continue
        table, kilde = match
        print(f"  leser {path.name} ...", flush=True)
        raw, header = read_bris_excel(path)
        df, meta = clean(raw, header)
        df.insert(1, "kilde", kilde)
        df["kildefil"] = path.name
        meta.insert(0, "kildefil", path.name)
        tables.setdefault(table, []).append(df)
        metas.append(meta)
        print(f"    {len(df):,} rader", flush=True)

    out = {}
    for table, parts in tables.items():
        df = pd.concat(parts, ignore_index=True)
        before = len(df)
        df = df.drop_duplicates(subset=["oppdrag_id"], keep="first")
        if before != len(df):
            print(f"  {table}: fjernet {before - len(df):,} duplikater (samme oppdrag_id)")
        out[table] = df.sort_values("tidspunkt").reset_index(drop=True)
    out["kolonner"] = pd.concat(metas, ignore_index=True)
    return out


def main(raw_dir: Path = RAW_DIR) -> None:
    print(f"Leser BRIS-uttrekk fra {raw_dir}")
    tables = load_all(raw_dir)
    with db.connect() as con:
        for name, df in tables.items():
            db.write_table(df, name, con=con)
            print(f"  lagret tabell '{name}': {len(df):,} rader")
    print("Ferdig.")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else RAW_DIR)
