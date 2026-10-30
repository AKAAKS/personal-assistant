"""jarvisd entry point: python -m jarvisd [--no-speech] [--verbose]"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from jarvisd.bootstrap import create_assistant
from jarvisd.ha_events import HAEventListener
from jarvisd.notifier import Notifier
from jarvisd.server import JarvisServer


def _ensure_std_streams():
    """Under pythonw (the tray launches the daemon windowless) there is no
    console, so sys.stdout/stderr are None and the first print() in startup
    would crash the daemon before it binds the port. Point them at the log file
    so prints are captured instead of fatal."""
    Path("logs").mkdir(exist_ok=True)
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open("logs/jarvisd.log", "a", encoding="utf-8",
                                    buffering=1))


def setup_logging(verbose: bool):
    Path("logs").mkdir(exist_ok=True)
    handlers = [logging.FileHandler("logs/jarvisd.log", encoding="utf-8")]
    if sys.stderr is not None:  # no console under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    if not verbose:
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)
        # A loopback WS server gets bare TCP probes (the tray's liveness check,
        # port scanners); each logs a full "opening handshake failed" traceback.
        # These are benign — a real client's failures surface client-side — so
        # don't let them flood the log.
        logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


def main():
    parser = argparse.ArgumentParser(description="JARVIS brain daemon")
    parser.add_argument("--no-speech", action="store_true", help="skip loading STT/TTS models")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)  # must run while stderr is still None (no console)
    _ensure_std_streams()        # then make print() safe under pythonw
    log = logging.getLogger("jarvisd")

    try:
        run(args, log)
    except KeyboardInterrupt:
        print("\njarvisd stopped.")
    except OSError as e:
        log.error("Cannot bind %s:%s (%s) — is another jarvisd already running?",
                  os.getenv("JARVIS_HOST", "127.0.0.1"), os.getenv("JARVIS_PORT", "8765"), e)
        raise SystemExit(1)
    except Exception:
        log.exception("jarvisd crashed during startup")
        raise SystemExit(1)


def run(args, log):
    ai, features, conn, reminder_store, rules_store, ha, meal_store, proposal_store, habit_scan = create_assistant()

    tts_backend = os.getenv("TTS_BACKEND", "pyttsx3")
    stt = tts = None
    if not args.no_speech:
        # Importing faster-whisper/ctranslate2/torch + loading the CUDA model is a
        # ~15s cold operation that prints nothing until it's underway. Announce it
        # first (flushed) so this silent gap doesn't look like a hang worth Ctrl+C.
        print("Loading speech models (Whisper + TTS)... first start can take ~15s.", flush=True)
        from voice.stt import SpeechToText
        from voice.tts import make_tts
        stt = SpeechToText(
            model_size=os.getenv("WHISPER_MODEL", "small"),
            device=os.getenv("WHISPER_DEVICE", "cuda"),
            compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8_float16"),
        )
        tts = make_tts(
            tts_backend,
            rate=int(os.getenv("TTS_RATE", "180")),
            voice_id=os.getenv("TTS_VOICE_ID") or None,
        )
        features.append(f"TTS ({tts_backend})")
        from voice.speaker_verify import from_env as build_speaker_verifier
        speaker_verifier = build_speaker_verifier()
        if speaker_verifier is not None:
            features.append("Voice ID")
    else:
        speaker_verifier = None

    # Proactivity (reminders/notifier/HA rule triggers) needs a working DB conn;
    # bootstrap builds reminder_store/rules_store on that same conn all-or-nothing,
    # so `conn is not None` implies both stores are set too.
    notifier = None
    if conn is not None:
        notifier = Notifier(conn, ha_client=ha, quiet_hours=os.getenv("QUIET_HOURS", "23:00-08:00"))

    server = JarvisServer(
        ai, stt=stt, tts=tts, speaker_verifier=speaker_verifier,
        host=os.getenv("JARVIS_HOST", "127.0.0.1"),
        port=int(os.getenv("JARVIS_PORT", "8765")),
        notifier=notifier,
        reminder_store=reminder_store,
        meal_store=meal_store,
        proposal_store=proposal_store,
        habit_scan=habit_scan,
    )

    ha_token = os.getenv("HA_TOKEN")
    if notifier is not None and ha_token:
        # Guarded callables the HA event listener uses to fire rule actions. Both
        # take `server._lock` -- the exact lock JarvisServer._process holds around
        # every user turn -- so a rule firing mid-conversation serializes with user
        # turns and can't race the assistant's single conversation state. It must be
        # server._lock specifically (not assistant_lock_holder._lock) to match what
        # _process uses; they are the same object under the default holder. Built
        # here (not inside JarvisServer or HAEventListener) because they close over
        # both `ai` and `server._lock`, and the listener itself must stay
        # unit-testable without a real assistant.
        # autonomous=True: no user is present when a rule fires, so the guard
        # must treat destructive tools as terminally unconfirmable here (the
        # retry-confirm dance only makes sense on user-initiated turns).
        async def run_direct(action):
            async with server._lock:
                return await asyncio.to_thread(
                    ai.execute_guarded, action["tool"], action["input"], autonomous=True
                )

        async def run_smart(prompt):
            async with server._lock:
                return await asyncio.to_thread(ai.process, prompt, autonomous=True)

        ha_url = os.getenv("HA_URL", "http://localhost:8123")
        # Assigned post-construction (rather than passed into JarvisServer(...)
        # above) because run_direct/run_smart must close over `server._lock`,
        # which only exists once the server object itself has been built.
        # serve_forever() reads self.ha_listener when it starts, which happens
        # after this assignment, so the ordering is safe.
        server.ha_listener = HAEventListener(
            ha_url, ha_token, rules_store, notifier, run_direct, run_smart
        )

    print(f"\nJARVIS brain ready. Integrations: {', '.join(features)}")
    print(f"Budget: {ai.cost_tracker.summary()}")
    log.info("Integrations: %s | Budget: %s", ", ".join(features), ai.cost_tracker.summary())
    asyncio.run(server.serve_forever())


if __name__ == "__main__":
    main()
