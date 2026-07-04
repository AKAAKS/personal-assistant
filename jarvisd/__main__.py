"""jarvisd entry point: python -m jarvisd [--no-speech] [--verbose]"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from jarvisd.bootstrap import create_assistant
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
    ai, features = create_assistant()

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

    server = JarvisServer(
        ai, stt=stt, tts=tts,
        host=os.getenv("JARVIS_HOST", "127.0.0.1"),
        port=int(os.getenv("JARVIS_PORT", "8765")),
    )
    print(f"\nJARVIS brain ready. Integrations: {', '.join(features)}")
    print(f"Budget: {ai.cost_tracker.summary()}")
    log.info("Integrations: %s | Budget: %s", ", ".join(features), ai.cost_tracker.summary())
    asyncio.run(server.serve_forever())


if __name__ == "__main__":
    main()
