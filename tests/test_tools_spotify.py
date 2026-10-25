"""Spotify tool handlers — focus on the 'no active device' auto-recovery."""

from jarvisd.tools.spotify import _NO_DEVICE_MSG, handlers


class FakeSpotify:
    """Minimal SpotifyClient stand-in. Simulates the real 404-on-no-device
    behavior: play()/add_to_queue() raise unless a device_id is supplied."""

    def __init__(self, devices=None, play_needs_device=False, queue_needs_device=False):
        self._devices = devices or []
        self.play_needs_device = play_needs_device
        self.queue_needs_device = queue_needs_device
        self.play_calls = []
        self.queue_calls = []

    def get_devices(self):
        return list(self._devices)

    def play(self, uri=None, device_id=None):
        self.play_calls.append({"uri": uri, "device_id": device_id})
        if self.play_needs_device and device_id is None:
            raise RuntimeError("404 Client Error: Not Found for url: .../me/player/play")

    def add_to_queue(self, uri, device_id=None):
        self.queue_calls.append({"uri": uri, "device_id": device_id})
        if self.queue_needs_device and device_id is None:
            raise RuntimeError("404 Client Error: Not Found for url: .../me/player/queue")


def test_play_targets_the_active_device():
    sp = FakeSpotify(devices=[{"id": "d1", "name": "Laptop", "is_active": True}])
    out = handlers(sp)["spotify_play"]({"uri": "spotify:track:x"})
    assert "Now playing spotify:track:x" in out
    assert "On: Laptop" in out
    assert len(sp.play_calls) == 1                  # resolved up front — a single play
    assert sp.play_calls[0]["device_id"] == "d1"    # targeted directly, never a bare None


def test_play_targets_an_available_device_when_none_is_active():
    # A bare /me/player/play with no *active* device 204-no-ops silently, so the
    # handler resolves a concrete device up front rather than firing blind.
    sp = FakeSpotify(
        devices=[{"id": "d1", "name": "Laptop", "is_active": False}],
        play_needs_device=True,
    )
    out = handlers(sp)["spotify_play"]({"uri": "spotify:track:x"})
    assert "Now playing spotify:track:x" in out
    assert "On: Laptop" in out
    assert len(sp.play_calls) == 1                  # one call, aimed at the device
    assert sp.play_calls[0]["device_id"] == "d1"    # no failed None attempt first


def test_play_prefers_the_active_device():
    sp = FakeSpotify(
        devices=[
            {"id": "d1", "name": "Phone", "is_active": False},
            {"id": "d2", "name": "Speaker", "is_active": True},
        ],
        play_needs_device=True,
    )
    out = handlers(sp)["spotify_play"]({"uri": "spotify:track:x"})
    assert "On: Speaker" in out
    assert sp.play_calls[-1]["device_id"] == "d2"


def test_play_gives_clear_message_when_no_devices_exist():
    sp = FakeSpotify(devices=[], play_needs_device=True)
    out = handlers(sp)["spotify_play"]({"uri": "spotify:track:x"})
    assert out == _NO_DEVICE_MSG  # a helpful message, not a raw 404 / exception


def test_queue_recovers_by_targeting_a_device():
    sp = FakeSpotify(
        devices=[{"id": "d1", "name": "Laptop", "is_active": True}],
        queue_needs_device=True,
    )
    out = handlers(sp)["spotify_queue"]({"uri": "spotify:track:x"})
    assert "Added to queue" in out and "Laptop" in out
    assert sp.queue_calls[-1]["device_id"] == "d1"


def test_queue_gives_clear_message_when_no_devices_exist():
    sp = FakeSpotify(devices=[], queue_needs_device=True)
    out = handlers(sp)["spotify_queue"]({"uri": "spotify:track:x"})
    assert out == _NO_DEVICE_MSG
