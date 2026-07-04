"""Builds a fully wired Assistant from .env configuration."""

import os
import sys

from dotenv import load_dotenv

from ha_client import HomeAssistantClient
from jarvisd.core import Assistant
from spotify_client import SpotifyClient
from tv_client import AndroidTVClient


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
            spotify.ensure_authorized()
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
