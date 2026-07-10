"""Builds a fully wired Assistant from .env configuration."""

import os
import sys

from dotenv import load_dotenv

from ha_client import HomeAssistantClient
from jarvisd import db
from jarvisd.core import HAIKU_MODEL, Assistant
from jarvisd.memory_store import MemoryStore
from jarvisd.profile_store import ProfileStore
from jarvisd.reminder_store import ReminderStore
from jarvisd.rules_store import RulesStore
from spotify_client import SpotifyClient
from tv_client import AndroidTVClient


def create_assistant():
    """Initialize clients and create assistant.

    Returns (assistant, active_features, conn, reminder_store, rules_store, ha_client).
    conn/reminder_store/rules_store are None together if the DB/memory layer failed to
    initialize (all-or-nothing, same as the Phase 2 memory wiring). ha_client is always
    the same HomeAssistantClient instance already wired into the assistant -- returned
    so __main__ can reuse it for the Notifier and HA event listener without opening a
    second connection.
    """
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

    # Wire up memory + proactivity stores (all on the same conn; all-or-nothing)
    conn = None
    memory_store = None
    profile_store = None
    reminder_store = None
    rules_store = None
    try:
        conn = db.connect(os.getenv("JARVIS_DB", "data/jarvis.db"))
        memory_store = MemoryStore(conn)
        profile_store = ProfileStore()
        reminder_store = ReminderStore(conn)
        rules_store = RulesStore(conn)
    except Exception as e:
        print(f"Memory unavailable: {e}")
        conn = None
        memory_store = None
        profile_store = None
        reminder_store = None
        rules_store = None

    ai = Assistant(
        ha, anthropic_key,
        spotify_client=spotify,
        tv_client=tv,
        monthly_budget=budget,
        ollama_model=ollama_model,
        profile_store=profile_store,
        memory_store=memory_store,
        reminder_store=reminder_store,
        rules_store=rules_store,
    )

    def summarizer(text: str) -> str:
        """Condense long documents: free local Ollama when up, else one Haiku call.

        Used by the file tools (jarvisd/tools/files.py) to summarize documents
        too long to return in full. NOTE: not yet threaded into build_registry --
        Assistant does not accept file_tools/file_summarizer yet (that wiring is
        a separate task); this closure is defined here so it is ready once it does.
        """
        prompt = "Summarize the key points of this document in <=300 words:\n\n" + text
        if ai.ollama.is_available():
            try:
                return ai.ollama.generate(prompt)
            except Exception:
                pass
        resp = ai.client.messages.create(
            model=HAIKU_MODEL, max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        u = resp.usage
        ai.cost_tracker.record(HAIKU_MODEL, u.input_tokens, u.output_tokens, source="summarize")
        return "".join(b.text for b in resp.content if b.type == "text")

    features = ["Home Assistant", "PC Control"]
    if spotify:
        features.append("Spotify")
    if tv:
        features.append("Android TV")
    if memory_store and profile_store:
        features.append("Memory")
    if reminder_store and rules_store:
        features.append("Proactivity")

    return ai, features, conn, reminder_store, rules_store, ha
