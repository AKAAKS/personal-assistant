"""LocationProvider — current location from Windows GPS with a home fallback.

The real GPS read (winsdk) and reverse-geocode (HTTP) are injected, so these
tests use fakes and need neither Windows location nor the network.
"""

from location_provider import Location, LocationProvider


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
