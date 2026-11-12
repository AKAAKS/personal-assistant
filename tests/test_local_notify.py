"""The local interrupt: a Windows toast plus an audible chime.

This is the fallback the Notifier reaches for when no voice client is connected,
so it must be the most robust link in the chain: it can never raise into the
reminder tick, and it has to report honestly whether it actually got the user's
attention so routing can fall through to the phone push when it didn't.

The toast and chime backends are injected so the behaviour is testable without
making noise or depending on the host OS.
"""

from jarvisd.local_notify import make_local_notifier


class Spy:
    def __init__(self, raises=False):
        self.raises = raises
        self.calls = []

    def __call__(self, *args):
        if self.raises:
            raise RuntimeError("backend unavailable")
        self.calls.append(args)


# --- availability ----------------------------------------------------------


def test_returns_none_off_windows():
    """Linux/macOS have no winsound or winotify; the Notifier then skips this step."""
    assert make_local_notifier(platform="linux", toast=Spy(), chime=Spy()) is None


def test_returns_none_when_no_backend_is_available():
    """Nothing to call means nothing to offer -- don't hand back a no-op that
    claims success, or the phone push would never be tried."""
    assert make_local_notifier(platform="win32", toast=None, chime=None) is None


def test_available_with_only_a_chime():
    """A bare beep still interrupts, which is the whole job."""
    assert make_local_notifier(platform="win32", toast=None, chime=Spy()) is not None


# --- delivery --------------------------------------------------------------


def test_shows_the_toast_and_sounds_the_chime():
    toast, chime = Spy(), Spy()
    notify = make_local_notifier(platform="win32", toast=toast, chime=chime)

    assert notify("Timer's up.") is True
    assert toast.calls == [("JARVIS", "Timer's up.")]
    assert len(chime.calls) == 1


def test_reports_success_when_the_toast_fails_but_the_chime_sounds():
    """A silent desktop with an audible beep is still an interruption, so this
    must NOT fall through to the phone push."""
    toast, chime = Spy(raises=True), Spy()
    notify = make_local_notifier(platform="win32", toast=toast, chime=chime)

    assert notify("Timer's up.") is True
    assert len(chime.calls) == 1


def test_reports_success_when_the_chime_fails_but_the_toast_shows():
    toast, chime = Spy(), Spy(raises=True)
    notify = make_local_notifier(platform="win32", toast=toast, chime=chime)

    assert notify("Timer's up.") is True
    assert toast.calls == [("JARVIS", "Timer's up.")]


def test_reports_failure_when_both_backends_fail():
    """Nothing reached the user, so routing must continue to the phone push."""
    notify = make_local_notifier(
        platform="win32", toast=Spy(raises=True), chime=Spy(raises=True)
    )

    assert notify("Timer's up.") is False


def test_never_raises_whatever_the_backends_do():
    """notify() runs inside the reminder tick; an exception here would be logged
    as a failed tick and the reminder would be retried forever."""
    notify = make_local_notifier(
        platform="win32", toast=Spy(raises=True), chime=Spy(raises=True)
    )
    notify("Timer's up.")  # must not raise


def test_chime_sounds_even_when_the_toast_hangs_up_first():
    """Order matters: the audible cue must not be gated on the toast working."""
    calls = []
    toast = lambda *a: calls.append("toast") or (_ for _ in ()).throw(RuntimeError("x"))
    chime = lambda *a: calls.append("chime")
    notify = make_local_notifier(platform="win32", toast=toast, chime=chime)

    notify("Timer's up.")

    assert "chime" in calls
