"""get_weather tool — Open-Meteo lookup using the current location or a named place.

The HTTP calls (forecast fetch, geocode) are injected, so no network is needed.
"""

from location_provider import Location
from jarvisd.tools.weather import handlers


class FakeProvider:
    def __init__(self, loc):
        self._loc = loc

    def current(self):
        return self._loc


# A minimal Open-Meteo-shaped forecast payload.
def _payload(temp=4.0, code=3):
    return {
        "current": {
            "temperature_2m": temp, "apparent_temperature": temp - 3,
            "weather_code": code, "wind_speed_10m": 11.0, "relative_humidity_2m": 80,
        },
        "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
        "daily": {"temperature_2m_max": [6.0], "temperature_2m_min": [1.0]},
    }


def _handlers(loc=None, fetch=None, geocode=None):
    return handlers(
        FakeProvider(loc),
        fetch=fetch or (lambda lat, lon, units: _payload()),
        geocode=geocode or (lambda name: None),
    )


def test_get_weather_uses_current_location():
    loc = Location(43.65, -79.38, "Toronto", 42.0, "gps")
    seen = {}

    def fetch(lat, lon, units):
        seen["coords"] = (lat, lon)
        return _payload(temp=4.0, code=3)

    out = _handlers(loc=loc, fetch=fetch)["get_weather"]({})
    assert seen["coords"] == (43.65, -79.38)
    assert "Toronto" in out
    assert "4" in out and "overcast" in out.lower()  # code 3 -> overcast


def test_get_weather_named_place_is_geocoded():
    def geocode(name):
        assert name == "London"
        return (51.5, -0.12, "London, England, GB")

    out = _handlers(loc=None, geocode=geocode)["get_weather"]({"location": "London"})
    assert "London" in out


def test_get_weather_without_location_is_graceful():
    out = _handlers(loc=None)["get_weather"]({})
    assert "location" in out.lower()


def test_get_weather_unknown_place():
    out = _handlers(loc=None, geocode=lambda name: None)["get_weather"]({"location": "Narnia"})
    assert "narnia" in out.lower() or "find" in out.lower()


def test_get_weather_handles_fetch_error():
    def boom(lat, lon, units):
        raise RuntimeError("open-meteo down")

    loc = Location(1.0, 2.0, "Somewhere", None, "gps")
    out = _handlers(loc=loc, fetch=boom)["get_weather"]({})
    assert "weather" in out.lower() and "down" in out.lower()
