import requests

from src.config import FROST_CLIENT_ID


FROST_OBSERVATIONS_URL = "https://frost.met.no/observations/v0.jsonld"


def fetch_observations(
    source: str,
    elements: list[str],
    start_date: str,
    end_date: str,
):
    params = {
        "sources": source,
        "elements": ",".join(elements),
        "referencetime": f"{start_date}/{end_date}",
    }

    response = requests.get(
        FROST_OBSERVATIONS_URL,
        params=params,
        auth=(FROST_CLIENT_ID, ""),
        timeout=30,
    )

    response.raise_for_status()

    return response.json()