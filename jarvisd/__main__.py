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


def main():
    parser = argparse.ArgumentParser(description="JARVIS brain daemon")
    parser.add_argument("--no-speech", action="store_true", help="skip loading STT/TTS models")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
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
    ai, features, conn, reminder_store, rules_store, ha, meal_store = create_assistant()

    stt = tts = None
    if not args.no_speech:
        from voice.stt import SpeechToText
        from voice.tts import TextToSpeech
        stt = SpeechToText(
            model_size=os.getenv("WHISPER_MODEL", "small"),
            device=os.getenv("WHISPER_DEVICE", "cuda"),
            compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8_float16"),
        )
        tts = TextToSpeech(rate=int(os.getenv("TTS_RATE", "180")))

    # Proactivity (reminders/notifier/HA rule triggers) needs a working DB conn;
    # bootstrap builds reminder_store/rules_store on that same conn all-or-nothing,
    # so `conn is not None` implies both stores are set too.
    notifier = None
    if conn is not None:
        notifier = Notifier(conn, ha_client=ha, quiet_hours=os.getenv("QUIET_HOURS", "23:00-08:00"))

    server = JarvisServer(
        ai, stt=stt, tts=tts,
        host=os.getenv("JARVIS_HOST", "127.0.0.1"),
        port=int(os.getenv("JARVIS_PORT", "8765")),
        notifier=notifier,
        reminder_store=reminder_store,
        meal_store=meal_store,
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
