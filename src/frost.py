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


def parse_hourly_observations(data):
    rows = []

    for item in data["data"]:
        timestamp = item["referenceTime"]

        row = {
            "timestamp": timestamp,
            "air_temperature": None,
            "relative_humidity": None,
            "wind_speed": None,
        }

        for obs in item["observations"]:
            element = obs["elementId"]
            resolution = obs.get("timeResolution")
            level = obs.get("level", {}).get("value")

            # We only use hourly observations
            if resolution != "PT1H":
                continue

            if element == "air_temperature" and level == 2:
                row["air_temperature"] = obs["value"]

            elif element == "relative_humidity" and level == 2:
                row["relative_humidity"] = obs["value"]

            elif element == "wind_speed" and level == 10:
                row["wind_speed"] = obs["value"]

        if any(
            row[key] is not None
            for key in ["air_temperature", "relative_humidity", "wind_speed"]
        ):
            rows.append(row)

    return rows