"""Current location for JARVIS, so it never has to ask "where are you?".

Reads precise coordinates from the Windows Location API (winsdk), reverse-geocodes
them to a place name, and caches the result. Falls back to a configured home
location when Windows location is off/unavailable, so weather and "near me"
queries still work. The GPS read and the reverse-geocode HTTP call are injected,
keeping the logic testable without Windows or the network.

The daemon builds this via `from_env()` and calls `start()` to refresh it on a
background thread; `current()` is a cheap, non-blocking cache read consumed by
the system prompt (jarvisd/core.py) and the weather tool.
"""

import os
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    place: str | None
    accuracy_m: float | None
    source: str  # "gps" or "home"


class LocationProvider:
    def __init__(self, *, read_gps, reverse_geocode=None, home=None):
        """
        Args:
            read_gps: callable() -> (lat, lon, accuracy_m) | None.
            reverse_geocode: callable(lat, lon) -> place name | None (best-effort).
            home: a Location to fall back to when GPS is unavailable, or None.
        """
        self._read_gps = read_gps
        self._reverse_geocode = reverse_geocode or (lambda lat, lon: None)
        self._home = home
        self._cached = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def refresh(self) -> Location | None:
        """Re-read the location now and update the cache. Never raises."""
        try:
            gps = self._read_gps()
        except Exception:
            gps = None

        loc = None
        if gps is not None:
            lat, lon, accuracy = gps
            try:
                place = self._reverse_geocode(lat, lon)
            except Exception:
                place = None
            loc = Location(lat, lon, place, accuracy, "gps")
        elif self._home is not None:
            loc = self._home

        with self._lock:
            self._cached = loc
        return loc

    def current(self) -> Location | None:
        """Last refreshed location (None until the first refresh). Non-blocking."""
        with self._lock:
            return self._cached

    def start(self, interval_sec: float = 600.0) -> threading.Thread:
        """Refresh immediately, then keep refreshing on a daemon thread."""
        def loop():
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(interval_sec)
        t = threading.Thread(target=loop, daemon=True, name="location-refresh")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    # -- real collaborators (used by from_env; not exercised by unit tests) --

    @classmethod
    def from_env(cls) -> "LocationProvider":
        """Build a provider wired to Windows GPS + reverse geocode, with the
        JARVIS_HOME_* env vars as the fallback."""
        home = None
        lat, lon = os.getenv("JARVIS_HOME_LAT"), os.getenv("JARVIS_HOME_LON")
        if lat and lon:
            try:
                home = Location(
                    float(lat), float(lon),
                    os.getenv("JARVIS_HOME_NAME") or "home", None, "home",
                )
            except ValueError:
                home = None
        return cls(read_gps=_read_windows_gps, reverse_geocode=_reverse_geocode, home=home)


def _read_windows_gps():
    """Precise coordinates via the Windows Location API. Returns (lat, lon,
    accuracy_m) or None if location is off/denied/unavailable."""
    import asyncio

    from winsdk.windows.devices.geolocation import (
        GeolocationAccessStatus,
        Geolocator,
        PositionAccuracy,
    )

    async def _go():
        if await Geolocator.request_access_async() != GeolocationAccessStatus.ALLOWED:
            return None
        geo = Geolocator()
        geo.desired_accuracy = PositionAccuracy.DEFAULT
        pos = await geo.get_geoposition_async()
        p = pos.coordinate.point.position
        return (p.latitude, p.longitude, pos.coordinate.accuracy)

    return asyncio.run(_go())


def _reverse_geocode(lat, lon):
    """Best-effort city name from coordinates (keyless, free). None on failure."""
    import requests

    r = requests.get(
        "https://api.bigdatacloud.net/data/reverse-geocode-client",
        params={"latitude": lat, "longitude": lon, "localityLanguage": "en"},
        timeout=5,
    )
    r.raise_for_status()
    d = r.json()
    return d.get("city") or d.get("locality") or d.get("principalSubdivision") or None
