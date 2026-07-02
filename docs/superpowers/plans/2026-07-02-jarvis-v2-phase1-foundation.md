# JARVIS v2 Phase 1 — Foundation Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the working v1 assistant into an always-on brain daemon (`jarvisd`) with a WebSocket API plus two thin clients (voice, CLI), with tests, SQLite foundation, refreshed models, and auto-start — at feature parity with v1.

**Architecture:** The 770-line `assistant.py` splits into `jarvisd/` (agent core, tool registry per domain, WebSocket server, SQLite module) while device clients (`ha_client.py` etc.), `router/`, `security/`, and the reusable `voice/` pieces stay put. STT/TTS move into the brain; clients only do wake-word/mic/playback. Spec: `docs/superpowers/specs/2026-07-02-jarvis-v2-design.md`.

**Tech Stack:** Python 3.11, `anthropic`, `websockets` (new), `pytest` (new, dev), SQLite (stdlib), faster-whisper/pyttsx3/openwakeword (existing).

## Global Constraints

- Windows 11; commands below are Git Bash unless stated otherwise.
- Models: default tier `claude-haiku-4-5`, complex tier `claude-sonnet-5` (replaces `claude-sonnet-4-6`). No other model IDs.
- Budget machinery unchanged in behavior: $20/month default, force-downgrade at 95%.
- WebSocket protocol: JSON text frames + raw binary frames; audio is 16 kHz mono int16 PCM (client→brain) and WAV bytes (brain→client). Default bind `127.0.0.1:8765`, overridable via `JARVIS_HOST`/`JARVIS_PORT`.
- Never commit `.env`, `logs/`, or `data/` (SQLite lives in `data/jarvis.db`).
- All work happens on branch `phase1-foundation` off `master`.
- Run tests with plain `pytest` from the repo root. Tests must not hit the network or load ML models (except the one real-TTS test marked in Task 7).
- Working dir for all commands: `C:/Users/akshi/Projects/personal-assistant`.

---

### Task 0: Branch

**Files:** none

- [ ] **Step 1: Create the working branch**

```bash
git checkout -b phase1-foundation
```

---

### Task 1: Test scaffolding + characterization tests

Lock in current behavior of the pure-logic modules before any refactor.

**Files:**
- Create: `conftest.py` (empty — makes pytest put repo root on `sys.path`)
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_classifier.py`
- Create: `tests/test_guard.py`
- Modify: `requirements.txt` (append dev deps)

**Interfaces:**
- Consumes: `router.classifier.IntentClassifier.classify(text) -> Tier`; `security.guard.SecurityGuard.check(tool_name, tool_input) -> SecurityResult`
- Produces: nothing new — safety net only.

- [ ] **Step 1: Add dev dependencies**

Append to `requirements.txt`:

```
# Networking (brain/client split)
websockets>=12.0

# Dev / test
pytest>=8.0
```

Run: `pip install -r requirements.txt`

- [ ] **Step 2: Create empty `conftest.py` and `tests/__init__.py`**

Both files empty. `conftest.py` at repo root ensures `import router`, `import security`, `import jarvisd` work under pytest.

- [ ] **Step 3: Write characterization tests**

`tests/test_classifier.py`:

```python
"""Characterization tests: lock v1 routing behavior before refactor."""
from router.classifier import IntentClassifier, Tier

clf = IntentClassifier()


def test_greetings_go_local():
    for text in ("hey", "Hello!", "good morning", "thanks a lot", "ok cool"):
        assert clf.classify(text) == Tier.LOCAL, text


def test_simple_commands_go_haiku():
    for text in ("turn off the bedroom light", "play some jazz", "pause the music"):
        assert clf.classify(text) == Tier.HAIKU, text


def test_complex_chains_go_sonnet():
    assert clf.classify("set up movie night: dim the lights and then also open Netflix") == Tier.SONNET


def test_very_long_requests_go_sonnet():
    assert clf.classify("word " * 45) == Tier.SONNET


def test_short_non_action_goes_local():
    assert clf.classify("nice weather") == Tier.LOCAL
```

`tests/test_guard.py`:

```python
"""Characterization tests: lock v1 security guard behavior before refactor."""
from security.guard import SecurityGuard, Verdict


def test_read_only_tool_allowed():
    g = SecurityGuard()
    assert g.check("list_devices", {}).verdict == Verdict.ALLOW


def test_blocked_keyword():
    g = SecurityGuard()
    r = g.check("send_notification", {"message": "my ssn is 123"})
    assert r.verdict == Verdict.BLOCK


def test_blocked_banking_url():
    g = SecurityGuard()
    r = g.check("open_url_browser", {"url": "https://www.chase.com/login"})
    assert r.verdict == Verdict.BLOCK


def test_unknown_tool_defaults_to_write_allowed():
    g = SecurityGuard()
    assert g.check("some_future_tool", {"x": 1}).verdict == Verdict.ALLOW


def test_destructive_requires_confirm_then_allows():
    g = SecurityGuard()
    # No tool is marked destructive in policy yet; simulate via monkeypatched map.
    from security import policy
    policy.TOOL_PERMISSIONS["fake_destructive"] = "destructive"
    try:
        first = g.check("fake_destructive", {"a": 1})
        second = g.check("fake_destructive", {"a": 1})
        assert first.verdict == Verdict.CONFIRM_REQUIRED
        assert second.verdict == Verdict.ALLOW
    finally:
        del policy.TOOL_PERMISSIONS["fake_destructive"]
```

- [ ] **Step 4: Run and verify all pass**

Run: `pytest tests/ -v`
Expected: all PASS (these document current behavior; a failure means the test is wrong — fix the test, not the code).

- [ ] **Step 5: Commit**

```bash
git add conftest.py tests/ requirements.txt
git commit -m "test: characterization tests for classifier and guard; add pytest + websockets deps"
```

---

### Task 2: Cache-aware, source-tagged cost tracker + model refresh pricing

**Files:**
- Modify: `router/cost_tracker.py`
- Test: `tests/test_cost_tracker.py`

**Interfaces:**
- Produces: `CostTracker.record(model, input_tokens, output_tokens, cache_read_tokens=0, cache_creation_tokens=0, source="interactive") -> float`. Existing callers with 3 positional args keep working. `MODEL_PRICING` keys: `claude-sonnet-5`, `claude-haiku-4-5`.

- [ ] **Step 1: Write the failing test**

`tests/test_cost_tracker.py`:

```python
import json
import pytest
from router.cost_tracker import CostTracker, MODEL_PRICING


def make(tmp_path):
    return CostTracker(log_path=str(tmp_path / "cost.jsonl"), monthly_budget=20.0)


def test_sonnet5_in_pricing_table():
    assert "claude-sonnet-5" in MODEL_PRICING
    assert "claude-sonnet-4-6" not in MODEL_PRICING


def test_cache_aware_cost(tmp_path):
    t = make(tmp_path)
    cost = t.record(
        "claude-haiku-4-5", 1000, 100,
        cache_read_tokens=10_000, cache_creation_tokens=2_000,
    )
    # uncached in @ $1 + cache reads @ 0.1x + cache writes @ 1.25x + out @ $5, per MTok
    expected = (1000 * 1.0 + 10_000 * 1.0 * 0.1 + 2_000 * 1.0 * 1.25 + 100 * 5.0) / 1_000_000
    assert cost == pytest.approx(expected)


def test_backward_compatible_three_arg_call(tmp_path):
    t = make(tmp_path)
    cost = t.record("claude-haiku-4-5", 1000, 100)
    assert cost == pytest.approx((1000 * 1.0 + 100 * 5.0) / 1_000_000)


def test_entry_carries_source_and_cache_fields(tmp_path):
    t = make(tmp_path)
    t.record("claude-haiku-4-5", 10, 5, cache_read_tokens=7, source="proactive")
    entry = json.loads((tmp_path / "cost.jsonl").read_text().strip())
    assert entry["source"] == "proactive"
    assert entry["cache_read"] == 7
    assert entry["cache_write"] == 0


def test_monthly_spend_tolerates_old_format_lines(tmp_path):
    p = tmp_path / "cost.jsonl"
    p.write_text('{"ts": "2099-01-01T00:00:00", "model": "x", "in": 1, "out": 1, "cost": 0.5}\n')
    t = CostTracker(log_path=str(p), monthly_budget=20.0)
    assert t.get_monthly_spend() >= 0.0  # must not raise
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_cost_tracker.py -v`
Expected: FAIL — `test_sonnet5_in_pricing_table` (key missing) and `test_cache_aware_cost` (unexpected keyword `cache_read_tokens`).

- [ ] **Step 3: Implement**

In `router/cost_tracker.py`, replace the `MODEL_PRICING` block and the `record` method:

```python
MODEL_PRICING = {
    # Standard list prices per MTok. (Sonnet 5 has intro pricing of $2/$10
    # through 2026-08-31; we track at list price — conservative overestimate.)
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

CACHE_READ_MULT = 0.1    # cache reads bill at ~0.1x input price
CACHE_WRITE_MULT = 1.25  # 5-minute-TTL cache writes bill at 1.25x input price
```

```python
    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        source: str = "interactive",
    ) -> float:
        pricing = MODEL_PRICING.get(model, {"input": 3.0, "output": 15.0})
        cost = (
            input_tokens * pricing["input"]
            + cache_read_tokens * pricing["input"] * CACHE_READ_MULT
            + cache_creation_tokens * pricing["input"] * CACHE_WRITE_MULT
            + output_tokens * pricing["output"]
        ) / 1_000_000

        entry = {
            "ts": datetime.now().isoformat(),
            "model": model,
            "in": input_tokens,
            "out": output_tokens,
            "cache_read": cache_read_tokens,
            "cache_write": cache_creation_tokens,
            "source": source,
            "cost": round(cost, 6),
        }

        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        self._month_cache = None

        over, spend, msg = self.check_budget()
        if msg:
            log.warning(msg)

        return cost
```

(Everything else in the file — `get_monthly_spend`, `check_budget`, `should_force_downgrade`, `summary` — is unchanged; it only reads `ts` and `cost`.)

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_cost_tracker.py -v`
Expected: all PASS. Also run `pytest tests/ -v` — everything still green.

- [ ] **Step 5: Commit**

```bash
git add router/cost_tracker.py tests/test_cost_tracker.py
git commit -m "feat(router): cache-aware, source-tagged cost tracking; Sonnet 5 pricing"
```

---

### Task 3: `jarvisd` package + SQLite module

**Files:**
- Create: `jarvisd/__init__.py` (empty)
- Create: `jarvisd/db.py`
- Test: `tests/test_db.py`
- Modify: `.gitignore` (append `data/`)

**Interfaces:**
- Produces: `jarvisd.db.connect(db_path="data/jarvis.db") -> sqlite3.Connection` (row_factory=`sqlite3.Row`, WAL mode, migrations applied). `jarvisd.db.SCHEMA_VERSION: int`. Later phases add tables by appending to `MIGRATIONS` and bumping `SCHEMA_VERSION`.

- [ ] **Step 1: Write the failing test**

`tests/test_db.py`:

```python
import sqlite3
from jarvisd import db


def test_connect_creates_file_and_schema(tmp_path):
    path = tmp_path / "sub" / "jarvis.db"
    conn = db.connect(str(path))
    assert path.exists()
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == db.SCHEMA_VERSION
    conn.close()


def test_connect_is_idempotent(tmp_path):
    path = str(tmp_path / "jarvis.db")
    db.connect(path).close()
    conn = db.connect(path)  # must not re-run migrations / raise
    count = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    assert count == db.SCHEMA_VERSION
    conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvisd'`.

- [ ] **Step 3: Implement**

`jarvisd/__init__.py`: empty.

`jarvisd/db.py`:

```python
"""SQLite storage for jarvisd — one database, versioned migrations."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# version -> SQL script. Later phases append entries and bump SCHEMA_VERSION.
MIGRATIONS = {
    1: "-- baseline: no tables yet; memory/meals/reminders arrive in later phases",
}


def connect(db_path: str = "data/jarvis.db") -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.executescript(MIGRATIONS[version])
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
```

Append `data/` on its own line to `.gitignore`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_db.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvisd/ tests/test_db.py .gitignore
git commit -m "feat(jarvisd): package skeleton + versioned SQLite module"
```

---

### Task 4: Tool registry — split tool definitions/handlers into `jarvisd/tools/`

Pure restructuring of `assistant.py`'s tool layer. No behavior change.

**Files:**
- Create: `jarvisd/tools/__init__.py`
- Create: `jarvisd/tools/ha.py`
- Create: `jarvisd/tools/spotify.py`
- Create: `jarvisd/tools/tv.py`
- Create: `jarvisd/tools/system.py`
- Test: `tests/test_tools_registry.py`
- (Do NOT touch `assistant.py` yet — it is deleted in Task 5.)

**Interfaces:**
- Consumes: device client methods exactly as used in `assistant.py` `_execute_tool` (lines 482–669).
- Produces: each module exports `TOOLS: list[dict]` and `handlers(client) -> dict[str, Callable[[dict], str]]`. Package exports `build_registry(ha_client, system_client, spotify_client=None, tv_client=None) -> tuple[list[dict], dict[str, Callable]]`.

- [ ] **Step 1: Write the failing test**

`tests/test_tools_registry.py`:

```python
from types import SimpleNamespace
from jarvisd.tools import build_registry


class FakeHA:
    def get_all_entities(self):
        return [{"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Desk"}}]

    def get_entities_by_domain(self, domain):
        return self.get_all_entities()

    def get_entity_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    def call_service(self, domain, action, entity_id, data=None):
        self.last = (domain, action, entity_id, data)

    def send_notification(self, message, title):
        pass


class FakeSystem:
    def close_app(self, name): return f"closed {name}"
    def launch_app(self, target): return f"launched {target}"
    def list_processes(self): return "procs"
    def open_url(self, url): return f"opened {url}"
    def system_info(self): return "info"


def test_registry_without_optional_clients():
    tools, handlers = build_registry(FakeHA(), FakeSystem())
    names = [t["name"] for t in tools]
    assert "list_devices" in names and "launch_app" in names
    assert "spotify_play" not in names and "tv_remote" not in names
    assert set(names) == set(handlers.keys())          # every tool has a handler
    assert len(names) == len(set(names))               # no duplicate names


def test_registry_with_optional_clients():
    spotify = SimpleNamespace()  # handlers close over it; not called here
    tv = SimpleNamespace()
    tools, handlers = build_registry(FakeHA(), FakeSystem(), spotify_client=spotify, tv_client=tv)
    names = [t["name"] for t in tools]
    assert "spotify_play" in names and "tv_remote" in names
    assert set(names) == set(handlers.keys())


def test_handler_dispatch_and_client_call():
    ha = FakeHA()
    tools, handlers = build_registry(ha, FakeSystem())
    out = handlers["list_devices"]({})
    assert "Desk" in out and "light.desk" in out
    handlers["control_light"]({"entity_id": "light.desk", "action": "turn_on", "brightness": 128})
    assert ha.last == ("light", "turn_on", "light.desk", {"brightness": 128})


def test_every_tool_def_is_well_formed():
    tools, _ = build_registry(FakeHA(), FakeSystem(), spotify_client=object(), tv_client=object())
    for t in tools:
        assert t["name"] and t["description"], t
        assert t["input_schema"]["type"] == "object", t["name"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_tools_registry.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_registry'`.

- [ ] **Step 3: Create the four domain modules**

Each module starts with the `TOOLS` list **moved verbatim** from `assistant.py` (copy the list literal, renamed to `TOOLS`):

- `jarvisd/tools/ha.py` ← `HA_TOOLS` (assistant.py lines 21–150)
- `jarvisd/tools/spotify.py` ← `SPOTIFY_TOOLS` (lines 152–240)
- `jarvisd/tools/tv.py` ← `TV_TOOLS` (lines 242–347)
- `jarvisd/tools/system.py` ← `SYSTEM_TOOLS` (lines 349–410)

Then add the `handlers()` factory to each. Full handler code:

`jarvisd/tools/ha.py` (after `TOOLS`):

```python
import json


def handlers(ha):
    """Home Assistant tool handlers closing over an HomeAssistantClient."""

    def list_devices(tool_input):
        domain = tool_input.get("domain")
        entities = ha.get_entities_by_domain(domain) if domain else ha.get_all_entities()
        summary = []
        for e in entities:
            attrs = e.get("attributes", {})
            name = attrs.get("friendly_name", e["entity_id"])
            summary.append(f"- {name} ({e['entity_id']}): {e['state']}")
        return "\n".join(summary) if summary else "No devices found."

    def get_device_status(tool_input):
        state = ha.get_entity_state(tool_input["entity_id"])
        attrs = state.get("attributes", {})
        name = attrs.get("friendly_name", state["entity_id"])
        return json.dumps({"name": name, "state": state["state"], "attributes": attrs}, indent=2)

    def control_light(tool_input):
        data = {}
        if "brightness" in tool_input:
            data["brightness"] = tool_input["brightness"]
        if "color_name" in tool_input:
            data["color_name"] = tool_input["color_name"]
        ha.call_service("light", tool_input["action"], tool_input["entity_id"], data or None)
        return f"Successfully called {tool_input['action']} on {tool_input['entity_id']}."

    def control_switch(tool_input):
        ha.call_service("switch", tool_input["action"], tool_input["entity_id"])
        return f"Successfully called {tool_input['action']} on {tool_input['entity_id']}."

    def control_media(tool_input):
        data = {}
        if "volume_level" in tool_input:
            data["volume_level"] = tool_input["volume_level"]
        ha.call_service("media_player", tool_input["action"], tool_input["entity_id"], data or None)
        return f"Successfully called {tool_input['action']} on {tool_input['entity_id']}."

    def set_thermostat(tool_input):
        entity_id = tool_input["entity_id"]
        if "hvac_mode" in tool_input:
            ha.call_service("climate", "set_hvac_mode", entity_id, {"hvac_mode": tool_input["hvac_mode"]})
        ha.call_service("climate", "set_temperature", entity_id, {"temperature": tool_input["temperature"]})
        return f"Thermostat {entity_id} set to {tool_input['temperature']} degrees."

    def send_notification(tool_input):
        title = tool_input.get("title", "Assistant")
        ha.send_notification(tool_input["message"], title)
        return f"Notification sent: {title} — {tool_input['message']}"

    return {
        "list_devices": list_devices,
        "get_device_status": get_device_status,
        "control_light": control_light,
        "control_switch": control_switch,
        "control_media": control_media,
        "set_thermostat": set_thermostat,
        "send_notification": send_notification,
    }
```

`jarvisd/tools/spotify.py` (after `TOOLS`):

```python
import json


def _find_device(spotify, device_name):
    for d in spotify.get_devices():
        if device_name.lower() in d["name"].lower():
            return d["id"]
    return None


def handlers(spotify):
    """Spotify tool handlers closing over a SpotifyClient."""

    def spotify_search(tool_input):
        query = tool_input["query"]
        search_type = tool_input.get("search_type", "track")
        results = spotify.search(query, search_type)
        if not results:
            return f"No {search_type} results found for '{query}'."
        return json.dumps(results, indent=2)

    def spotify_play(tool_input):
        uri = tool_input.get("uri")
        device_name = tool_input.get("device_name")
        device_id = None
        if device_name:
            device_id = _find_device(spotify, device_name)
            if not device_id:
                return (f"Could not find Spotify device '{device_name}'. "
                        "Use spotify_devices to list available devices.")
        spotify.play(uri=uri, device_id=device_id)
        msg = "Resumed playback." if not uri else f"Now playing {uri}."
        if device_name:
            msg += f" On: {device_name}."
        return msg

    def spotify_playback_control(tool_input):
        action = tool_input.get("action")
        volume = tool_input.get("volume_percent")
        if volume is not None:
            spotify.set_volume(volume)
            return f"Volume set to {volume}%."
        if action == "pause":
            spotify.pause()
            return "Playback paused."
        elif action == "next":
            spotify.next_track()
            return "Skipped to next track."
        elif action == "previous":
            spotify.previous_track()
            return "Went to previous track."
        elif action == "shuffle_on":
            spotify.set_shuffle(True)
            return "Shuffle enabled."
        elif action == "shuffle_off":
            spotify.set_shuffle(False)
            return "Shuffle disabled."
        elif action and action.startswith("repeat_"):
            mode = action.replace("repeat_", "")
            spotify.set_repeat(mode)
            return f"Repeat set to: {mode}."
        return "No action specified."

    def spotify_queue(tool_input):
        spotify.add_to_queue(tool_input["uri"])
        return f"Added to queue: {tool_input['uri']}"

    def spotify_now_playing(tool_input):
        info = spotify.get_current_playback()
        if not info:
            return "Nothing is currently playing on Spotify."
        return json.dumps(info, indent=2)

    def spotify_devices(tool_input):
        devices = spotify.get_devices()
        if not devices:
            return "No Spotify Connect devices found. Make sure Spotify is open on at least one device."
        return json.dumps(devices, indent=2)

    return {
        "spotify_search": spotify_search,
        "spotify_play": spotify_play,
        "spotify_playback_control": spotify_playback_control,
        "spotify_queue": spotify_queue,
        "spotify_now_playing": spotify_now_playing,
        "spotify_devices": spotify_devices,
    }
```

`jarvisd/tools/tv.py` (after `TOOLS`):

```python
import json


def handlers(tv):
    """Android TV tool handlers closing over an AndroidTVClient."""

    def tv_launch_app(tool_input):
        return tv.launch_app(tool_input["app_name"])

    def tv_close_app(tool_input):
        return tv.close_app(tool_input["app_name"])

    def tv_remote(tool_input):
        keys = tool_input.get("keys")
        key = tool_input.get("key")
        if keys:
            return tv.send_keys(keys)
        elif key:
            return tv.send_key(key)
        return "No key specified."

    def tv_type_text(tool_input):
        if tool_input.get("clear_first"):
            tv.clear_text()
        return tv.type_text(tool_input["text"])

    def tv_open_url(tool_input):
        return tv.open_url(tool_input["url"])

    def tv_search_youtube(tool_input):
        return tv.search_youtube(tool_input["query"])

    def tv_status(tool_input):
        return json.dumps({"screen": tv.get_screen_state(), "current_app": tv.get_current_app()}, indent=2)

    def tv_power(tool_input):
        if tool_input["action"] == "wake":
            return tv.wake_up()
        return tv.sleep()

    return {
        "tv_launch_app": tv_launch_app,
        "tv_close_app": tv_close_app,
        "tv_remote": tv_remote,
        "tv_type_text": tv_type_text,
        "tv_open_url": tv_open_url,
        "tv_search_youtube": tv_search_youtube,
        "tv_status": tv_status,
        "tv_power": tv_power,
    }
```

`jarvisd/tools/system.py` (after `TOOLS`):

```python
def handlers(system):
    """PC control tool handlers closing over a SystemClient."""
    return {
        "close_app": lambda ti: system.close_app(ti["name"]),
        "launch_app": lambda ti: system.launch_app(ti["target"]),
        "list_processes": lambda ti: system.list_processes(),
        "open_url_browser": lambda ti: system.open_url(ti["url"]),
        "system_info": lambda ti: system.system_info(),
    }
```

- [ ] **Step 4: Create the registry**

`jarvisd/tools/__init__.py`:

```python
"""Tool registry — assembles definitions + handlers for the available clients."""

from jarvisd.tools import ha, spotify, system, tv


def build_registry(ha_client, system_client, spotify_client=None, tv_client=None):
    """Return (tools, handlers) for the clients that exist.

    tools: list of Anthropic tool definitions (dicts)
    handlers: tool name -> callable(tool_input: dict) -> str
    """
    tools = list(ha.TOOLS) + list(system.TOOLS)
    handler_map = {}
    handler_map.update(ha.handlers(ha_client))
    handler_map.update(system.handlers(system_client))
    if spotify_client is not None:
        tools += list(spotify.TOOLS)
        handler_map.update(spotify.handlers(spotify_client))
    if tv_client is not None:
        tools += list(tv.TOOLS)
        handler_map.update(tv.handlers(tv_client))
    return tools, handler_map
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_tools_registry.py -v` — PASS. Then `pytest tests/ -v` — all green.

- [ ] **Step 6: Commit**

```bash
git add jarvisd/tools/ tests/test_tools_registry.py
git commit -m "refactor: extract tool definitions + handlers into jarvisd/tools registry"
```

---

### Task 5: `jarvisd/core.py` + `jarvisd/bootstrap.py`; retire `assistant.py`

**Files:**
- Create: `jarvisd/core.py`
- Create: `jarvisd/bootstrap.py`
- Modify: `main.py` (imports only)
- Delete: `assistant.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `build_registry` (Task 4), `CostTracker.record(..., cache_read_tokens=, cache_creation_tokens=)` (Task 2).
- Produces: `jarvisd.core.Assistant` — same public surface as v1 (`process(text) -> str`, `reset_conversation()`, `.cost_tracker`, `.ollama`) so `main.py` and the server use it unchanged. `jarvisd.bootstrap.create_assistant() -> tuple[Assistant, list[str]]`.

- [ ] **Step 1: Write the failing test**

`tests/test_core.py`:

```python
from tests.test_tools_registry import FakeHA, FakeSystem


def make_assistant():
    from jarvisd.core import Assistant
    return Assistant(FakeHA(), "sk-test-not-a-real-key", system_client=FakeSystem())


def test_tools_built_from_registry():
    ai = make_assistant()
    names = [t["name"] for t in ai.tools]
    assert "list_devices" in names
    assert "spotify_play" not in names  # no spotify client passed


def test_execute_tool_dispatch():
    ai = make_assistant()
    assert "Desk" in ai._execute_tool("list_devices", {})
    assert ai._execute_tool("nope_tool", {}) == "Unknown tool: nope_tool"


def test_execute_tool_wraps_exceptions():
    ai = make_assistant()
    out = ai._execute_tool("control_light", {})  # missing required keys -> KeyError inside
    assert out.startswith("Error executing control_light")


def test_models_are_current():
    from jarvisd import core
    assert core.HAIKU_MODEL == "claude-haiku-4-5"
    assert core.SONNET_MODEL == "claude-sonnet-5"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_core.py -v` — FAIL (`No module named 'jarvisd.core'`).

- [ ] **Step 3: Write `jarvisd/core.py`**

Copy `SYSTEM_PROMPT` verbatim from `assistant.py` lines 414–432 into `jarvisd/core.py`, then the class:

```python
"""Claude-powered agent core: routing, cost tracking, tool-use loop."""

import logging

import anthropic

from jarvisd.tools import build_registry
from router.classifier import IntentClassifier, Tier
from router.cost_tracker import CostTracker
from router.ollama_backend import OllamaBackend
from security.audit import AuditLogger
from security.guard import SecurityGuard, Verdict
from system_client import SystemClient

log = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5"
SONNET_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """<moved verbatim from assistant.py lines 414-432>"""


class Assistant:

    MAX_HISTORY = 40

    def __init__(
        self,
        ha_client,
        anthropic_api_key: str,
        spotify_client=None,
        tv_client=None,
        system_client=None,
        monthly_budget: float = 20.0,
        ollama_model: str = "qwen2.5:3b-instruct-q4_K_M",
    ):
        spotify = None
        if spotify_client is not None and getattr(spotify_client, "is_authenticated", lambda: True)():
            spotify = spotify_client

        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.conversation: list[dict] = []
        self.guard = SecurityGuard()
        self.audit = AuditLogger()
        self.classifier = IntentClassifier()
        self.cost_tracker = CostTracker(monthly_budget=monthly_budget)
        self.ollama = OllamaBackend(model=ollama_model)

        self.tools, self._handlers = build_registry(
            ha_client,
            system_client if system_client is not None else SystemClient(),
            spotify_client=spotify,
            tv_client=tv_client,
        )
        if self.tools:
            self.tools[-1] = {**self.tools[-1], "cache_control": {"type": "ephemeral"}}

        self._system_prompt = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        try:
            return handler(tool_input)
        except Exception as e:
            return f"Error executing {tool_name}: {e}"
```

Then move these methods from `assistant.py` **verbatim** (lines 671–772): `process`, `_handle_local`, `_claude_loop`, `_compact_history`, `reset_conversation` — with exactly two edits inside them:

1. In `process`, the model selection line becomes:
   ```python
   model = HAIKU_MODEL if tier == Tier.HAIKU else SONNET_MODEL
   ```
2. In `_claude_loop`, the `cost_tracker.record(...)` call becomes:
   ```python
   u = response.usage
   self.cost_tracker.record(
       model, u.input_tokens, u.output_tokens,
       cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
       cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
   )
   ```

- [ ] **Step 4: Write `jarvisd/bootstrap.py`**

Move `create_assistant()` from `main.py` (lines 26–86) verbatim, changing only the import (`from jarvisd.core import Assistant`) and keeping the same env handling. Full file:

```python
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
```

- [ ] **Step 5: Slim `main.py` and delete `assistant.py`**

In `main.py`: delete the moved `create_assistant()` and its now-unused imports; add `from jarvisd.bootstrap import create_assistant`. `run_text_mode` / `run_voice_mode` / `main()` stay untouched (v1 entry keeps working until Task 9).

```bash
git rm assistant.py
```

- [ ] **Step 6: Run tests + parity check**

Run: `pytest tests/ -v` — all PASS.
Parity check (uses real .env; needs HA reachable): `python main.py` then type `hey` (expect canned local reply, $0) and `quit`.

- [ ] **Step 7: Commit**

```bash
git add jarvisd/core.py jarvisd/bootstrap.py main.py tests/test_core.py
git commit -m "refactor: move agent core into jarvisd.core with tool registry; retire assistant.py"
```

---

### Task 6: WebSocket server (text protocol)

**Files:**
- Create: `jarvisd/server.py`
- Create: `jarvisd/__main__.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: any object with `.process(text) -> str` and `.reset_conversation()` (the `Assistant`); optional `stt` with `.transcribe(np.ndarray) -> str`; optional `tts` with `.synthesize(text) -> bytes | None` (Task 7 adds it).
- Produces: `jarvisd.server.JarvisServer(assistant, stt=None, tts=None, host="127.0.0.1", port=8765)` with `async handle(ws)` and `async serve_forever()`. Protocol (used by Tasks 8–9):
  - client→brain JSON: `{"type": "text_in", "text": str, "speak": bool?}`, `{"type": "reset"}`
  - client→brain binary: one utterance, 16 kHz mono int16 PCM
  - brain→client JSON: `{"type": "reply", "text": str, "heard": str|null, "audio_follows": bool}` or `{"type": "error", "error": str}`
  - brain→client binary: WAV bytes, only immediately after a reply with `audio_follows: true`

- [ ] **Step 1: Write the failing test**

`tests/test_server.py`:

```python
import asyncio
import json

import websockets

from jarvisd.server import JarvisServer


class FakeAssistant:
    def __init__(self):
        self.was_reset = False

    def process(self, text):
        return f"echo: {text}"

    def reset_conversation(self):
        self.was_reset = True


def run_client(server, scenario):
    """Start server on an ephemeral port, run `scenario(ws)` against it."""
    async def _run():
        async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                return await scenario(ws)
    return asyncio.run(_run())


def test_text_round_trip():
    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant()), scenario)
    assert reply == {"type": "reply", "text": "echo: hi", "heard": None, "audio_follows": False}


def test_reset():
    fake = FakeAssistant()

    async def scenario(ws):
        await ws.send(json.dumps({"type": "reset"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(fake), scenario)
    assert reply["type"] == "reply"
    assert fake.was_reset


def test_bad_json_and_unknown_type():
    async def scenario(ws):
        await ws.send("{not json")
        first = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "warp_drive"}))
        second = json.loads(await ws.recv())
        return first, second

    first, second = run_client(JarvisServer(FakeAssistant()), scenario)
    assert first["type"] == "error"
    assert second["type"] == "error"


def test_audio_without_stt_is_an_error():
    async def scenario(ws):
        await ws.send(b"\x00\x00" * 100)
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant()), scenario)
    assert reply["type"] == "error"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_server.py -v` — FAIL (`No module named 'jarvisd.server'`).

- [ ] **Step 3: Implement `jarvisd/server.py`**

```python
"""WebSocket server exposing the assistant to thin clients.

Protocol:
  client -> brain  JSON  {"type": "text_in", "text": str, "speak": bool?}
                   JSON  {"type": "reset"}
                   binary one utterance of 16 kHz mono int16 PCM
  brain -> client  JSON  {"type": "reply", "text": str, "heard": str|null,
                          "audio_follows": bool}
                   JSON  {"type": "error", "error": str}
                   binary WAV bytes (only right after audio_follows=true)
"""

import asyncio
import json
import logging

import websockets

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 32 * 1024 * 1024  # 15s of 16kHz int16 is ~0.5MB; generous cap


class JarvisServer:

    def __init__(self, assistant, stt=None, tts=None, host="127.0.0.1", port=8765):
        self.assistant = assistant
        self.stt = stt
        self.tts = tts
        self.host = host
        self.port = port
        self._lock = asyncio.Lock()  # one conversation; serialize agent access

    async def serve_forever(self):
        async with websockets.serve(self.handle, self.host, self.port, max_size=MAX_MESSAGE_BYTES):
            log.info("jarvisd listening on ws://%s:%d", self.host, self.port)
            await asyncio.Future()

    async def handle(self, ws):
        async for message in ws:
            try:
                if isinstance(message, (bytes, bytearray)):
                    await self._handle_audio(ws, bytes(message))
                else:
                    await self._handle_json(ws, message)
            except Exception as e:
                log.exception("request failed")
                await ws.send(json.dumps({"type": "error", "error": str(e)}))

    async def _process(self, text: str) -> str:
        async with self._lock:
            return await asyncio.to_thread(self.assistant.process, text)

    async def _handle_json(self, ws, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({"type": "error", "error": "invalid JSON"}))
            return
        mtype = msg.get("type")
        if mtype == "text_in":
            reply = await self._process(msg.get("text", ""))
            await self._send_reply(ws, reply, heard=None, speak=bool(msg.get("speak")))
        elif mtype == "reset":
            self.assistant.reset_conversation()
            await ws.send(json.dumps(
                {"type": "reply", "text": "Conversation cleared.", "heard": None, "audio_follows": False}
            ))
        else:
            await ws.send(json.dumps({"type": "error", "error": f"unknown message type: {mtype!r}"}))

    async def _handle_audio(self, ws, pcm: bytes):
        if self.stt is None:
            await ws.send(json.dumps({"type": "error", "error": "speech not enabled on this server"}))
            return
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16)
        heard = await asyncio.to_thread(self.stt.transcribe, audio)
        if not heard.strip():
            await ws.send(json.dumps({"type": "reply", "text": "", "heard": "", "audio_follows": False}))
            return
        reply = await self._process(heard)
        await self._send_reply(ws, reply, heard=heard, speak=True)

    async def _send_reply(self, ws, reply: str, heard, speak: bool):
        wav = None
        if speak and self.tts is not None and reply:
            wav = await asyncio.to_thread(self.tts.synthesize, reply)
        await ws.send(json.dumps(
            {"type": "reply", "text": reply, "heard": heard, "audio_follows": wav is not None}
        ))
        if wav is not None:
            await ws.send(wav)
```

- [ ] **Step 4: Implement `jarvisd/__main__.py`**

```python
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
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_server.py -v` — PASS. `pytest tests/ -v` — all green.

- [ ] **Step 6: Commit**

```bash
git add jarvisd/server.py jarvisd/__main__.py tests/test_server.py
git commit -m "feat(jarvisd): WebSocket server + daemon entry point (text protocol)"
```

---

### Task 7: Audio path — `TextToSpeech.synthesize` + server audio round-trip

**Files:**
- Modify: `voice/tts.py` (add one method)
- Test: `tests/test_tts.py`, extend `tests/test_server.py`

**Interfaces:**
- Produces: `TextToSpeech.synthesize(text: str) -> bytes | None` — RIFF/WAV bytes, `None` for empty text. Server already calls it (Task 6); this task makes audio real.

- [ ] **Step 1: Write the failing tests**

`tests/test_tts.py`:

```python
import pytest


@pytest.mark.slow  # loads SAPI5; still offline and fast (~1s)
def test_synthesize_returns_wav_bytes():
    from voice.tts import TextToSpeech
    tts = TextToSpeech(rate=200)
    wav = tts.synthesize("hello")
    assert wav is not None and wav[:4] == b"RIFF"
    assert tts.synthesize("") is None
```

Append to `tests/test_server.py`:

```python
import numpy as np


class FakeSTT:
    def transcribe(self, audio):
        assert audio.dtype == np.int16
        return "turn on the light"


class FakeTTS:
    def synthesize(self, text):
        return b"RIFF-fake-wav:" + text.encode()


def test_audio_round_trip_with_speech():
    async def scenario(ws):
        await ws.send(np.zeros(16000, dtype=np.int16).tobytes())
        reply = json.loads(await ws.recv())
        wav = await ws.recv()
        return reply, wav

    server = JarvisServer(FakeAssistant(), stt=FakeSTT(), tts=FakeTTS())
    reply, wav = run_client(server, scenario)
    assert reply["heard"] == "turn on the light"
    assert reply["text"] == "echo: turn on the light"
    assert reply["audio_follows"] is True
    assert isinstance(wav, (bytes, bytearray)) and wav.startswith(b"RIFF-fake-wav:")


def test_silent_audio_returns_empty_reply():
    class SilentSTT:
        def transcribe(self, audio):
            return "   "

    async def scenario(ws):
        await ws.send(np.zeros(1600, dtype=np.int16).tobytes())
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(FakeAssistant(), stt=SilentSTT(), tts=FakeTTS()), scenario)
    assert reply["heard"] == "" and reply["audio_follows"] is False
```

Register the marker — create `pytest.ini`:

```ini
[pytest]
markers =
    slow: loads real speech engines
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_tts.py tests/test_server.py -v`
Expected: `test_synthesize_returns_wav_bytes` FAILS (`AttributeError: synthesize`); the two new server tests PASS already (server code landed in Task 6) — that's fine, they pin the contract.

- [ ] **Step 3: Implement `synthesize` in `voice/tts.py`**

```python
    def synthesize(self, text: str) -> bytes | None:
        """Render text to WAV bytes (SAPI5 writes RIFF WAV). None for empty text."""
        if not text:
            return None
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            self.engine.save_to_file(text, path)
            self.engine.runAndWait()
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.unlink(path)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/ -v` — all PASS (including the slow TTS test).

- [ ] **Step 5: Commit**

```bash
git add voice/tts.py tests/test_tts.py tests/test_server.py pytest.ini
git commit -m "feat(voice): TTS synthesize-to-WAV; audio round-trip covered by server tests"
```

---

### Task 8: CLI client

**Files:**
- Create: `clients/__init__.py` (empty)
- Create: `clients/cli.py`

**Interfaces:**
- Consumes: server protocol from Task 6.
- Produces: `python -m clients.cli` interactive typed chat. No tests (thin I/O shell; verified by smoke test).

- [ ] **Step 1: Implement `clients/cli.py`**

```python
"""Typed chat client for jarvisd.  Usage: python -m clients.cli"""

import asyncio
import json
import os
import sys

import websockets


async def chat():
    url = os.getenv("JARVIS_URL", "ws://127.0.0.1:8765")
    try:
        async with websockets.connect(url) as ws:
            print("Connected to jarvisd. 'quit' to exit, 'reset' to clear history.\n")
            while True:
                try:
                    user = input("You: ").strip()
                except (KeyboardInterrupt, EOFError):
                    break
                if not user:
                    continue
                if user.lower() in ("quit", "exit", "q"):
                    break
                msg = {"type": "reset"} if user.lower() == "reset" else {"type": "text_in", "text": user}
                await ws.send(json.dumps(msg))
                reply = json.loads(await ws.recv())
                if reply.get("audio_follows"):
                    await ws.recv()  # CLI discards audio
                print(f"\nJARVIS: {reply.get('text') or reply.get('error')}\n")
    except (ConnectionRefusedError, OSError):
        print(f"Cannot reach jarvisd at {url}. Start it with: python -m jarvisd")
        sys.exit(1)
    print("Bye.")


if __name__ == "__main__":
    asyncio.run(chat())
```

(Note: `input()` blocks the event loop — acceptable for a strictly request/reply CLI. When phase 3 adds server-push `proactive_say`, this client needs an async read loop; that's a phase 3 change.)

- [ ] **Step 2: Smoke test**

Terminal 1: `python -m jarvisd --no-speech`
Terminal 2: `python -m clients.cli`, type `hey` → expect the canned local greeting (zero API cost), then `quit`.

- [ ] **Step 3: Commit**

```bash
git add clients/
git commit -m "feat(clients): CLI chat client"
```

---

### Task 9: Thin voice client; retire `main.py` + `voice/voice_pipeline.py`

**Files:**
- Create: `clients/voice_client.py`
- Delete: `main.py`, `voice/voice_pipeline.py`

**Interfaces:**
- Consumes: `voice.audio_io.MicrophoneStream`/`VoiceActivityDetector`, `voice.wake_word.WakeWordDetector` (unchanged), server protocol from Task 6/7.
- Produces: `python -m clients.voice_client` — wake word + VAD locally, one PCM utterance per turn to the brain, prints `heard`/reply, plays returned WAV.

- [ ] **Step 1: Implement `clients/voice_client.py`**

```python
"""Thin voice client: wake word + VAD locally; audio to jarvisd; plays replies.

Run: python -m clients.voice_client
"""

import asyncio
import json
import logging
import os
import winsound

import numpy as np
import websockets

from voice.audio_io import MicrophoneStream, VoiceActivityDetector
from voice.wake_word import WakeWordDetector

log = logging.getLogger(__name__)

WAKE_WORD_CHUNK = 1280  # openwakeword expects 80ms chunks at 16kHz


class VoiceClient:

    def __init__(self, url: str):
        self.url = url
        print("Loading wake-word model...")
        self.mic = MicrophoneStream()
        self.vad = VoiceActivityDetector(aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")))
        self.wake_word = WakeWordDetector(
            model_name=os.getenv("WAKE_WORD_MODEL", "hey_jarvis_v0.1"),
            threshold=float(os.getenv("WAKE_WORD_THRESHOLD", "0.5")),
        )
        self._wake_buf = np.array([], dtype=np.int16)

    def _capture_utterance(self) -> bytes | None:
        """Block until wake word, then record until silence. PCM bytes or None."""
        while True:
            frame = self.mic.read_frame(timeout=1.0)
            if frame is None:
                continue
            self._wake_buf = np.concatenate([self._wake_buf, frame])
            if len(self._wake_buf) < WAKE_WORD_CHUNK:
                continue
            chunk = self._wake_buf[:WAKE_WORD_CHUNK]
            self._wake_buf = self._wake_buf[WAKE_WORD_CHUNK:]
            if not self.wake_word.process_frame(chunk):
                continue

            log.info("Wake word detected")
            try:
                winsound.Beep(800, 200)
            except Exception:
                pass
            self.mic.clear()
            self._wake_buf = np.array([], dtype=np.int16)

            audio = self.vad.record_until_silence(
                self.mic, max_seconds=15.0, silence_ms=800, speech_timeout_sec=5.0,
            )
            if audio is None or len(audio) < 1600:
                log.info("No speech after wake word")
                return None
            return audio.tobytes()

    async def run(self):
        self.mic.start()
        print("Voice client active. Listening for wake word... Ctrl+C to exit.\n")
        try:
            async with websockets.connect(self.url, max_size=32 * 1024 * 1024) as ws:
                while True:
                    pcm = await asyncio.to_thread(self._capture_utterance)
                    if pcm is None:
                        continue
                    await ws.send(pcm)
                    reply = json.loads(await ws.recv())
                    if reply.get("type") == "error":
                        print(f"  [error] {reply.get('error')}\n")
                        continue
                    if reply.get("heard"):
                        print(f"  You: {reply['heard']}")
                    if reply.get("text"):
                        print(f"  JARVIS: {reply['text']}\n")
                    if reply.get("audio_follows"):
                        wav = await ws.recv()
                        await asyncio.to_thread(
                            winsound.PlaySound, bytes(wav), winsound.SND_MEMORY
                        )
        finally:
            self.mic.stop()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    url = os.getenv("JARVIS_URL", "ws://127.0.0.1:8765")
    try:
        asyncio.run(VoiceClient(url).run())
    except KeyboardInterrupt:
        print("\nBye.")
    except (ConnectionRefusedError, OSError):
        print(f"Cannot reach jarvisd at {url}. Start it with: python -m jarvisd")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Delete superseded entry points**

```bash
git rm main.py voice/voice_pipeline.py
```

(`voice/stt.py`, `voice/tts.py`, `voice/audio_io.py`, `voice/wake_word.py` stay — stt/tts serve the brain, audio_io/wake_word serve clients.)

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v` — all PASS (nothing imported `main` or `voice_pipeline`).

- [ ] **Step 4: Live smoke test (feature parity gate)**

Terminal 1: `python -m jarvisd` (full speech). Terminal 2: `python -m clients.voice_client`.
Say "hey jarvis" → beep → speak a command → verify transcript prints, reply prints, and the reply is spoken through the speakers.

- [ ] **Step 5: Commit**

```bash
git add clients/voice_client.py
git commit -m "feat(clients): thin voice client; retire main.py and voice_pipeline"
```

---

### Task 10: Auto-start at login + README

**Files:**
- Create: `scripts/install_autostart.bat`
- Create: `scripts/uninstall_autostart.bat`
- Create: `README.md`

**Interfaces:** none — ops + docs.

- [ ] **Step 1: Create the autostart scripts**

`scripts/install_autostart.bat`:

```bat
@echo off
rem Registers jarvisd to start hidden at logon (Task Scheduler).
rem If you use a venv, edit PYTHONW below to point at <venv>\Scripts\pythonw.exe
set PROJ=%~dp0..
set PYTHONW=pythonw
schtasks /Create /F /TN "jarvisd" /SC ONLOGON ^
  /TR "cmd /c cd /d \"%PROJ%\" && %PYTHONW% -m jarvisd"
if %errorlevel%==0 (
  echo jarvisd registered. It will start at next logon.
  echo Start it now with:  schtasks /Run /TN "jarvisd"
) else (
  echo FAILED - run this script from an elevated prompt if access was denied.
)
```

`scripts/uninstall_autostart.bat`:

```bat
@echo off
schtasks /Delete /TN "jarvisd" /F
```

- [ ] **Step 2: Write `README.md`**

```markdown
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

## Always-on

    scripts\install_autostart.bat     # registers a logon task; logs -> logs/jarvisd.log
    scripts\uninstall_autostart.bat

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
```

- [ ] **Step 3: Verify autostart registration**

Run (cmd): `scripts\install_autostart.bat`, then `schtasks /Run /TN "jarvisd"`, wait ~15s, confirm `logs/jarvisd.log` shows "jarvisd listening", then `python -m clients.cli` connects. Leave the task installed.

- [ ] **Step 4: Commit and merge**

```bash
git add scripts/ README.md
git commit -m "feat: auto-start at login script + README"
git checkout master && git merge phase1-foundation
```

---

## Final acceptance checklist (manual, ~5 min)

1. `pytest` — all green.
2. Reboot or re-logon → `logs/jarvisd.log` shows the daemon came up by itself.
3. CLI: `hey` (local tier, $0) and one real command ("what devices do you see?") — check `logs/cost.jsonl` entry has `cache_read`/`source` fields and model `claude-haiku-4-5`.
4. Voice: "hey jarvis … turn on/off <a real device>" round-trips with spoken reply.
5. A complex chain ("movie night…") routes to `claude-sonnet-5` (visible in jarvisd log).
