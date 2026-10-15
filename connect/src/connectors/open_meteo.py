"""Weather for a saved place, from Open-Meteo (no key, no account)."""

import json
from urllib.parse import urlencode

HOST = "api.open-meteo.com"
DAILY = ("weather_code", "temperature_2m_max", "temperature_2m_min",
         "precipitation_probability_max", "wind_speed_10m_max")

# WMO weather interpretation codes, as Open-Meteo documents them, mapped to
# fixed phrases. The service's number is kept; the words are ours, so there
# is no free text from the service to carry.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def build(operation: str, params: dict) -> str:
    query = urlencode({
        "latitude": f"{params['lat']:.2f}",
        "longitude": f"{params['lon']:.2f}",
        "daily": ",".join(DAILY),
        "timezone": "Europe/London",
        "forecast_days": params["days"],
    })
    return f"https://{HOST}/v1/forecast?{query}"


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalise(operation: str, body: bytes, params: dict) -> list:
    data = json.loads(body)
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    out = []
    for i, day in enumerate(dates[: params["days"]]):
        if not isinstance(day, str) or len(day) != 10:
            continue

        def col(name):
            values = daily.get(name) or []
            return _num(values[i]) if i < len(values) else None

        code = col("weather_code")
        code = int(code) if code is not None else None
        out.append({
            "date": day,
            "code": code,
            "summary": WMO.get(code, "unrecognised weather code"),
            "temp_max_c": col("temperature_2m_max"),
            "temp_min_c": col("temperature_2m_min"),
            "rain_chance_pct": col("precipitation_probability_max"),
            "wind_max_kmh": col("wind_speed_10m_max"),
        })
    return out
