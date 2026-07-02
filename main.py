"""JARVIS Personal Assistant — text and voice interface."""

import argparse
import logging
import os
import sys
from dotenv import load_dotenv
from ha_client import HomeAssistantClient
from spotify_client import SpotifyClient
from tv_client import AndroidTVClient
from assistant import Assistant


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)
        logging.getLogger("openwakeword").setLevel(logging.WARNING)


def create_assistant() -> tuple[Assistant, list[str]]:
    """Initialize clients and create assistant. Returns (assistant, active_features)."""
    load_dotenv()

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    ha_url = os.getenv("HA_URL", "http://localhost:8123")
    ha_token = os.getenv("HA_TOKEN")

    if not anthropic_key:
        print("Error: ANTHROPIC_API_KEY not set.")
        sys.exit(1)
    if not ha_token:
        print("Error: HA_TOKEN not set.")
        sys.exit(1)

    ha = HomeAssistantClient(ha_url, ha_token)
    if ha.check_connection():
        print(f"Connected to Home Assistant at {ha_url}")
    else:
        print(f"Warning: Cannot reach Home Assistant at {ha_url}")

    spotify = None
    spotify_id = os.getenv("SPOTIFY_CLIENT_ID")
    spotify_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if spotify_id and spotify_secret:
        spotify = SpotifyClient(spotify_id, spotify_secret)
        try:
            spotify.authorize()
            print("Spotify connected!")
        except Exception as e:
            print(f"Spotify auth failed: {e}")
            spotify = None

    tv = None
    tv_ip = os.getenv("ANDROID_TV_IP")
    if tv_ip:
        tv = AndroidTVClient(tv_ip)
        try:
            print(f"Android TV: {tv.connect()}")
        except Exception as e:
            print(f"Android TV failed: {e}")
            tv = None

    budget = float(os.getenv("MONTHLY_BUDGET", "20"))
    ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct-q4_K_M")

    ai = Assistant(
        ha, anthropic_key,
        spotify_client=spotify,
        tv_client=tv,
        monthly_budget=budget,
        ollama_model=ollama_model,
    )

    features = ["Home Assistant", "PC Control"]
    if spotify:
        features.append("Spotify")
    if tv:
        features.append("Android TV")

    return ai, features


def run_text_mode(ai: Assistant):
    print("Type 'quit' to exit, 'reset' to clear history.\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break
        if user_input.lower() == "reset":
            ai.reset_conversation()
            print("Conversation history cleared.\n")
            continue

        try:
            response = ai.process(user_input)
            print(f"\nAssistant: {response}\n")
        except Exception as e:
            print(f"\nError: {e}\n")


def run_voice_mode(ai: Assistant):
    from voice.voice_pipeline import VoicePipeline

    config = {
        "wake_word_model": os.getenv("WAKE_WORD_MODEL", "hey_jarvis_v0.1"),
        "wake_word_threshold": float(os.getenv("WAKE_WORD_THRESHOLD", "0.5")),
        "whisper_model": os.getenv("WHISPER_MODEL", "small"),
        "whisper_device": os.getenv("WHISPER_DEVICE", "cuda"),
        "whisper_compute_type": os.getenv("WHISPER_COMPUTE_TYPE", "int8_float16"),
        "tts_rate": int(os.getenv("TTS_RATE", "180")),
        "vad_aggressiveness": int(os.getenv("VAD_AGGRESSIVENESS", "2")),
    }

    pipeline = VoicePipeline(ai, config)
    pipeline.run()


def main():
    parser = argparse.ArgumentParser(description="JARVIS Personal Assistant")
    parser.add_argument("--voice", action="store_true", help="Voice mode (wake word + speech)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    setup_logging(args.verbose)
    ai, features = create_assistant()

    ollama_status = "available" if ai.ollama.is_available() else "not installed"
    print(f"\nJARVIS ready! Integrations: {', '.join(features)}")
    print(f"Router: Haiku (default) / Sonnet (complex) / Local ({ollama_status})")
    print(f"Budget: {ai.cost_tracker.summary()}")

    if args.voice:
        run_voice_mode(ai)
    else:
        run_text_mode(ai)


if __name__ == "__main__":
    main()
