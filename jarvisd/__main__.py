"""jarvisd entry point: python -m jarvisd [--no-speech] [--verbose]"""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from jarvisd.bootstrap import create_assistant
from jarvisd.server import JarvisServer


def setup_logging(verbose: bool):
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/jarvisd.log", encoding="utf-8"),
        ],
    )
    if not verbose:
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="JARVIS brain daemon")
    parser.add_argument("--no-speech", action="store_true", help="skip loading STT/TTS models")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)

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
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        print("\njarvisd stopped.")


if __name__ == "__main__":
    main()
