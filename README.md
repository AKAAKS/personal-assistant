# JARVIS — personal assistant

Always-on brain daemon (`jarvisd`) + thin clients. Claude tool-use agent
controlling Home Assistant, Spotify, Android TV, and this PC, with tiered
Haiku/Sonnet/local routing, a $20/month budget guard, and a security layer.

Design docs: `docs/superpowers/specs/`.

## Setup

    pip install -r requirements.txt
    copy .env.example .env    # then fill in keys

## Run

    python -m jarvisd                 # the brain (add --no-speech to skip Whisper/TTS)
    python -m clients.cli             # typed chat
    python -m clients.voice_client    # "hey jarvis" voice mode

The daemon binds `127.0.0.1:8765` (override with `JARVIS_HOST`/`JARVIS_PORT`).
Warning: the WebSocket has no authentication — do not set `JARVIS_HOST` to a
non-loopback address; that would expose full HA/PC/TV control to your network.

## Memory

JARVIS remembers you and your preferences:

- **Profile** (`data/profile.md`): a curated file of facts the assistant always knows. Edit it
  directly; changes take effect immediately on the next request (no restart needed).
  Use "Remember that ..." to have JARVIS add a fact.
- **Recall**: ask "what did we talk about volume?" to search past conversations.
- **History**: conversation history is persisted in SQLite and restored on daemon restart,
  so JARVIS knows the context of recent interactions even after it restarts.

Note: The design spec calls for `memory/profile.md`, but we use `data/` because it is
already gitignored and holds all personal state.

## Always-on

    scripts\install_autostart.bat     # registers a logon task (does NOT start it)
    scripts\start_jarvisd.bat         # start it now; logs -> logs/jarvisd.log
    scripts\stop_jarvisd.bat          # stop it (ends the task AND frees the port)
    scripts\uninstall_autostart.bat

Only one daemon can hold the port: if `python -m jarvisd` reports "Cannot
bind", the scheduled task's instance is already running — stop it first.
Spotify auth opens a browser once; after that the refresh token is cached
in `data/spotify_tokens.json` and startup is silent.

## Tests

    pytest

Marker `slow` covers tests that load the real SAPI5 TTS engine:
`pytest -m "not slow"` skips them.

## Layout

    jarvisd/          brain: agent core, tool registry, WebSocket server, SQLite
    clients/          thin clients (CLI, voice)
    router/           intent classifier, cost tracker, Ollama backend
    security/         guard, policy, audit log
    voice/            STT/TTS (used by brain), mic/VAD/wake word (used by clients)
    *_client.py       device clients (Home Assistant, Spotify, Android TV, system)
