"""LocationProvider — current location from Windows GPS with a home fallback.

The real GPS read (winsdk) and reverse-geocode (HTTP) are injected, so these
tests use fakes and need neither Windows location nor the network.
"""

from location_provider import Location, LocationProvider, _format_osm_address


def _provider(gps=None, geocode=None, home=None, raise_gps=False, raise_geo=False):
    def read_gps():
        if raise_gps:
            raise RuntimeError("no location sensor")
        return gps

    def reverse_geocode(lat, lon):
        if raise_geo:
            raise RuntimeError("geocode down")
        return geocode

    return LocationProvider(read_gps=read_gps, reverse_geocode=reverse_geocode, home=home)


def test_refresh_uses_gps_with_reverse_geocoded_place():
    p = _provider(gps=(43.65, -79.38, 42.0), geocode="Toronto")
    loc = p.refresh()
    assert loc == Location(43.65, -79.38, "Toronto", 42.0, "gps")
    assert p.current() == loc


def test_refresh_falls_back_to_home_when_gps_unavailable():
    home = Location(51.5, -0.12, "Home", None, "home")
    p = _provider(gps=None, home=home)
    assert p.refresh() == home
    assert p.current().source == "home"


def test_refresh_returns_none_without_gps_or_home():
    p = _provider(gps=None, home=None)
    assert p.refresh() is None
    assert p.current() is None


def test_refresh_survives_gps_exception():
    home = Location(51.5, -0.12, "Home", None, "home")
    p = _provider(raise_gps=True, home=home)
    assert p.refresh() == home  # GPS blew up -> fall back, don't crash


def test_refresh_survives_reverse_geocode_failure():
    p = _provider(gps=(1.0, 2.0, 10.0), raise_geo=True)
    loc = p.refresh()
    assert loc.source == "gps"
    assert loc.place is None  # geocode failed, but we still have coordinates


def test_current_is_none_before_first_refresh():
    assert _provider(gps=(1.0, 2.0, 10.0)).current() is None


# --- street-level reverse-geocode parsing (OpenStreetMap / Nominatim) ---

def test_format_osm_address_full_street():
    data = {"address": {
        "house_number": "123", "road": "Main St",
        "neighbourhood": "Downtown", "city": "State College", "state": "Pennsylvania",
    }}
    assert _format_osm_address(data) == "123 Main St, Downtown, State College"


def test_format_osm_address_road_without_number():
    data = {"address": {"road": "Main St", "town": "State College"}}
    assert _format_osm_address(data) == "Main St, State College"


def test_format_osm_address_falls_back_to_display_name():
    data = {"address": {}, "display_name": "Ferguson Township, Centre County, PA"}
    assert _format_osm_address(data) == "Ferguson Township"


def test_format_osm_address_empty():
    assert _format_osm_address({}) is None


# --- explicit home from env ---

def test_from_env_reads_home(monkeypatch):
    monkeypatch.setenv("JARVIS_HOME_LAT", "40.80")
    monkeypatch.setenv("JARVIS_HOME_LON", "-77.86")
    monkeypatch.setenv("JARVIS_HOME_NAME", "123 Main St, State College")
    p = LocationProvider.from_env()
    assert p.home is not None
    assert p.home.place == "123 Main St, State College"
    assert p.home.source == "home"


def test_from_env_without_home(monkeypatch):
    monkeypatch.delenv("JARVIS_HOME_LAT", raising=False)
    monkeypatch.delenv("JARVIS_HOME_LON", raising=False)
    assert LocationProvider.from_env().home is None
