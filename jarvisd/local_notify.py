"""Interrupt the user on THIS machine: a Windows toast plus an audible chime.

The Notifier's other routes are both conditional. Speaking a notice aloud needs
a connected voice client, which only exists while the user has one running, and
the phone push needs a reachable Home Assistant. When a timer came due with
neither available, the notice was queued silently and surfaced only when the
user next spoke -- as "this passed while you were away". This module is the
route that is always there.

Both backends are injected so the routing logic can be tested without making
noise or requiring Windows. Nothing here raises: it is called from inside the
reminder tick, where an exception would be logged as a failed tick and the
reminder retried on every scan.
"""

import logging
import sys

log = logging.getLogger(__name__)

TITLE = "JARVIS"


def _winotify_toast(title: str, text: str) -> None:
    """Show a Windows toast that persists in the Action Center.

    Imported lazily: winotify is Windows-only, and the daemon must still start
    on a machine without it.
    """
    from winotify import Notification

    toast = Notification(app_id=TITLE, title=title, msg=text)
    toast.show()


def _winsound_chime() -> None:
    """The audible half. MessageBeep is non-blocking and needs no clip on disk."""
    import winsound

    winsound.MessageBeep(winsound.MB_ICONASTERISK)


def _default_toast():
    """The toast backend, or None when it cannot be imported."""
    try:
        import winotify  # noqa: F401
    except Exception:
        return None
    return _winotify_toast


def _default_chime():
    try:
        import winsound  # noqa: F401
    except Exception:
        return None
    return _winsound_chime


def make_local_notifier(*, platform: str | None = None, toast=..., chime=...):
    """Build the Notifier's local_notify callable, or None if unavailable.

    Returns callable(text) -> bool, where the bool reports whether the user was
    actually interrupted. Returning None (rather than a no-op that claims
    success) matters: a callable that always returned True would stop routing
    before the Home Assistant phone push ever ran.
    """
    if platform is None:
        platform = sys.platform
    if toast is ...:
        toast = _default_toast()
    if chime is ...:
        chime = _default_chime()

    if not platform.startswith("win"):
        return None
    if toast is None and chime is None:
        return None

    def local_notify(text: str) -> bool:
        reached = False
        # The chime is attempted independently of the toast, and its result is
        # ORed in: a beep with no visible toast is still an interruption, and a
        # toast on a muted machine is still one too. Only both failing means the
        # notice did not land and the phone push should be tried.
        if toast is not None:
            try:
                toast(TITLE, text)
                reached = True
            except Exception as e:
                log.warning("toast notification failed: %s", e)
        if chime is not None:
            try:
                chime()
                reached = True
            except Exception as e:
                log.warning("notification chime failed: %s", e)
        return reached

    return local_notify
