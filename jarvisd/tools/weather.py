"""Weather tool — free, keyless forecasts via Open-Meteo.

Uses JARVIS's current location by default (so the user never has to state where
they are), or an explicit place name they give. Replaces web search for weather:
instant, free, and off the monthly search budget. The forecast fetch and the
name->coords geocode are injected so the handler is testable without the network.
"""

import os

# WMO weather interpretation codes -> plain words.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}

TOOLS = [
    {
        "name": "get_weather",
        "description": (
            "Get the current weather and today's forecast. Defaults to the user's "
            "current location (no need to ask where they are); pass a place name only "
            "if they ask about somewhere else. Prefer this over web search for weather."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Optional place name, e.g. 'London' or 'Paris, France'. Omit for here.",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "Override the default unit system for this answer.",
                },
            },
            "required": [],
        },
    },
]


def _format(data, place, units):
    cur = data.get("current") or {}
    cu = data.get("current_units") or {}
    code = int(cur.get("weather_code", -1))
    desc = _WMO.get(code, "unknown conditions")
    tu = cu.get("temperature_2m", "°")
    parts = [f"{place}: {desc}, {cur.get('temperature_2m')}{tu}"]
    feels = cur.get("apparent_temperature")
    if feels is not None and feels != cur.get("temperature_2m"):
        parts.append(f"feels like {feels}{tu}")
    wind = cur.get("wind_speed_10m")
    if wind is not None:
        parts.append(f"wind {wind} {cu.get('wind_speed_10m', '')}".rstrip())
    daily = data.get("daily") or {}
    highs, lows = daily.get("temperature_2m_max") or [], daily.get("temperature_2m_min") or []
    if highs and lows:
        parts.append(f"today {lows[0]}–{highs[0]}{tu}")
    return ", ".join(str(p) for p in parts) + "."


def handlers(location_provider, *, fetch=None, geocode=None):
    """Weather handler closing over the location provider.

    Args:
        location_provider: object with current() -> Location | None (or None).
        fetch: callable(lat, lon, units) -> Open-Meteo forecast dict.
        geocode: callable(name) -> (lat, lon, place) | None.
    """
    fetch = fetch or _fetch
    geocode = geocode or _geocode

    def get_weather(ti):
        units = ti.get("units") or os.getenv("JARVIS_UNITS", "metric")
        place_arg = ti.get("location")

        if place_arg and place_arg.strip():
            try:
                g = geocode(place_arg.strip())
            except Exception as e:
                return f"Couldn't look up '{place_arg}': {e}"
            if not g:
                return f"Couldn't find a place called '{place_arg}'."
            lat, lon, place = g
        else:
            loc = location_provider.current() if location_provider is not None else None
            if loc is None:
                return ("I don't have your location yet — turn on Windows location access, "
                        "or tell me a place and I'll check there.")
            lat, lon, place = loc.latitude, loc.longitude, loc.place or "your location"

        try:
            data = fetch(lat, lon, units)
        except Exception as e:
            return f"Couldn't get the weather right now: {e}"
        return _format(data, place, units)

    return {"get_weather": get_weather}


# -- real collaborators (not exercised by unit tests) --

def _fetch(lat, lon, units):
    import requests

    metric = units != "imperial"
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "auto", "forecast_days": 2,
            "temperature_unit": "celsius" if metric else "fahrenheit",
            "wind_speed_unit": "kmh" if metric else "mph",
        },
        timeout=8,
    )
    r.raise_for_status()
    return r.json()


def _geocode(name):
    import requests

    r = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 1, "language": "en"},
        timeout=6,
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return None
    top = results[0]
    place = ", ".join(filter(None, [top.get("name"), top.get("admin1"), top.get("country_code")]))
    return (top["latitude"], top["longitude"], place)
