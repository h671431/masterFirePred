"""Enkel tilgang til DuckDB-databasen for prosjektet."""

from pathlib import Path

import duckdb
import pandas as pd

from src.config import DB_PATH


def connect(path: Path = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def write_table(df: pd.DataFrame, name: str, con=None, replace: bool = True) -> None:
    """Skriv en DataFrame til en tabell (erstatter som standard eksisterende tabell)."""
    own = con is None
    con = con or connect()
    try:
        con.register("_df", df)
        if replace:
            con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _df')
        else:
            exists = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
            ).fetchone()[0]
            if exists:
                con.execute(f'INSERT INTO "{name}" SELECT * FROM _df')
            else:
                con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _df')
        con.unregister("_df")
    finally:
        if own:
            con.close()


def query(sql: str, params=None, path: Path = DB_PATH) -> pd.DataFrame:
    """Kjør en SQL-spørring og returner resultatet som DataFrame."""
    with connect(path, read_only=True) as con:
        return con.execute(sql, params or []).df()
