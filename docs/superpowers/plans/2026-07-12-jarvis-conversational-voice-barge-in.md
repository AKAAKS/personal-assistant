# JARVIS Conversational Voice + Barge-In Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS a concise, dry, spoken-prose persona (no narrated markdown) and let the user interrupt a spoken reply by saying the wake word, with the model told when it was cut off.

**Architecture:** Two independent slices. (1) *Tone* — rewrite the system-prompt persona and add a pure `strip_markdown()` applied to every outgoing reply so display + TTS get clean prose. (2) *Barge-in* — replace blocking playback with async playback watched by the wake-word detector; on the wake word, stop audio and record the user's next utterance immediately; a `{"type":"interrupt"}` control frame tells the server to mark the last assistant turn so the model knows it was cut off.

**Tech Stack:** Python 3.11, `winsound` (Windows async playback), `wave`/`io` (WAV parsing), `websockets`, `pytest`, faster-whisper (unchanged), edge-tts/pyttsx3 (unchanged), Anthropic Messages API (Haiku/Sonnet, unchanged).

## Global Constraints

- Windows / PowerShell environment; audio via `winsound` only (no new deps).
- No new Python packages. No model or routing changes.
- Never weaken the security guard, audit choke point, or path/URL/keyword blocks.
- `strip_markdown` touches only the **outgoing reply copy** — never `assistant.conversation` or stored memory.
- WebSocket server stays loopback-bound (`127.0.0.1`) and unauthenticated by design; the new `interrupt` frame carries no data.
- Persona rule set is verbatim in the system prompt: no markdown, no lists, no headings, no bold/italics, no emoji; default one–two sentences; no "sir"/name honorific.
- Run the whole suite with `python -m pytest -q` from the repo root. Work on branch `feat/conversational-voice-barge-in` (already holds the spec commit).

---

### Task 1: `strip_markdown()` — spoken-prose sanitizer

**Files:**
- Create: `jarvisd/text_format.py`
- Test: `tests/test_text_format.py`

**Interfaces:**
- Produces: `strip_markdown(text: str) -> str` — removes bold/italic/heading/code/link markup and emoji, turns list items into plain sentences, leaves clean prose unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_text_format.py
from jarvisd.text_format import strip_markdown


def test_bold_and_italic_markers_removed():
    assert strip_markdown("**Tom Ellis** is an _actor_") == "Tom Ellis is an actor"


def test_headings_removed():
    assert strip_markdown("## Options") == "Options"


def test_list_items_become_sentences():
    out = strip_markdown("- A specific song\n- An artist")
    assert "-" not in out
    assert "A specific song." in out
    assert "An artist." in out


def test_numbered_list_items_become_sentences():
    out = strip_markdown("1. First\n2) Second")
    assert "First." in out and "Second." in out
    assert "1." not in out and "2)" not in out


def test_inline_code_and_links_unwrapped():
    assert strip_markdown("run `ls` now") == "run ls now"
    assert strip_markdown("see [Google](http://g.co)") == "see Google"


def test_emoji_dropped():
    assert strip_markdown("Enjoy! 🎵😊").strip() == "Enjoy!"


def test_plain_prose_unchanged():
    s = "Sure, what do you want to hear?"
    assert strip_markdown(s) == s


def test_empty_string_safe():
    assert strip_markdown("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_text_format.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvisd.text_format'`

- [ ] **Step 3: Write minimal implementation**

```python
# jarvisd/text_format.py
"""Turn model markdown into clean spoken prose for TTS + terminal display.

JARVIS replies are read aloud, so any markdown the model emits (bold, bullet
lists, headings, emoji) would otherwise be narrated verbatim. strip_markdown is
applied to the OUTGOING reply only -- never to conversation history.
"""

import re

# Broad emoji / pictograph / dingbat ranges + variation selector.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols, pictographs, emoticons, supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators
    "\U00002B00-\U00002BFF"  # misc symbols and arrows
    "️"                  # variation selector-16
    "]"
)


def strip_markdown(text: str) -> str:
    """Return `text` as plain spoken prose: markup removed, lists flattened."""
    if not text:
        return text

    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^#{1,6}\s*", "", line)  # "## Title" -> "Title"
        m = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", line)  # list markers
        if m:
            line = m.group(1).strip()
            if line and line[-1] not in ".!?:;,":
                line += "."  # give the TTS a sentence boundary to pause on
        lines.append(line)
    text = "\n".join(lines)

    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)              # __bold__
    text = re.sub(r"\*(.+?)\*", r"\1", text)              # *italic*
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)   # _italic_
    text = re.sub(r"`(.+?)`", r"\1", text)                # `code`
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", text)     # [text](url) -> text
    text = _EMOJI_RE.sub("", text)

    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_text_format.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add jarvisd/text_format.py tests/test_text_format.py
git commit -m "feat: add strip_markdown() to turn model markdown into spoken prose"
```

---

### Task 2: JARVIS persona in the system prompt

**Files:**
- Modify: `jarvisd/core.py:24-45` (the `SYSTEM_PROMPT` string)
- Test: `tests/test_core.py` (add one test)

**Interfaces:**
- Produces: `SYSTEM_PROMPT` now carries the persona + hard style rules. No signature change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_core.py  (add this test)
from jarvisd.core import SYSTEM_PROMPT


def test_system_prompt_carries_spoken_persona_rules():
    p = SYSTEM_PROMPT.lower()
    assert "no markdown" in p
    assert "no bullet" in p or "no lists" in p
    assert "read aloud" in p
    # never the butler honorific
    assert '"sir"' in SYSTEM_PROMPT or "never use \"sir\"" in SYSTEM_PROMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_core.py::test_system_prompt_carries_spoken_persona_rules -q`
Expected: FAIL on `assert "no markdown" in p`

- [ ] **Step 3: Replace the SYSTEM_PROMPT**

Replace the whole `SYSTEM_PROMPT = """..."""` block (currently `jarvisd/core.py:24-45`) with:

```python
SYSTEM_PROMPT = """You are JARVIS, the user's personal AI assistant. You control their smart home, Spotify, Android TV, and PC.

Persona and voice — this matters as much as being correct:
- You speak out loud through a voice assistant. Everything you say is READ ALOUD, so talk the way people talk, not the way documents are written.
- You are concise, dry, and quietly competent, with a touch of understated wit — a capable aide who's comfortable with the user. Warm, never gushing. Address the user directly; never use "sir" or their name as a title.
- Hard rules for every reply:
  - Plain spoken sentences only. NO markdown, NO bullet points, NO numbered lists, NO headings, NO bold or italics, NO emoji.
  - Default to one or two sentences. Only go longer when the user explicitly asks for detail.
  - Use contractions. Get to the point; skip preamble like "Sure, I can help with that!".
  - If you must convey several items, say them in a sentence ("You've got three: eggs, a run, and the dentist"), never as a list.
  - When you're unsure what the user meant, ask about the single most likely interpretation as a short question — don't enumerate every possibility.
  - If your previous reply was cut off (you'll see a note saying so), the user interrupted to correct or redirect you. Adjust to what they now want; don't repeat what you were saying.

Capabilities:
- Smart home (Home Assistant): lights, switches, climate, sensors, notifications
- Spotify: search, play on any device, control playback
- Android TV: launch/navigate apps, type in search fields, remote control
- PC control: launch/close apps, list processes, open URLs, system info
- Web search: current information from the web, with citations. Each search costs money — only search when the answer needs current information.
- Habit suggestions: when the user asks about suggestions, use list_proposals; accept_proposal/dismiss_proposal act on them.

Security policy (strictly enforced):
- NO access to bank accounts, cards, financial services, or sensitive IDs
- Banking/payment/trading URLs are blocked
- Destructive actions require user confirmation

How you work:
- Confirm actions briefly after doing them ("Done — lights are off.").
- Chain multiple actions when it makes sense (e.g. "movie night" = dim the lights and open Netflix).
- Spotify: search for the URI first, then play. Target a specific device only if asked.
- TV: launch the app first, then open its search, then type the query.
- "Give me my briefing" = weather plus top headlines (web search), today's reminders (list_reminders), and today's meals (meal_summary), delivered as a short spoken rundown.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_core.py tests/test_core_memory.py -q`
Expected: PASS (the new test passes; existing core tests still pass — `_build_system` structure is unchanged)

- [ ] **Step 5: Commit**

```bash
git add jarvisd/core.py tests/test_core.py
git commit -m "feat: give JARVIS a concise spoken-prose persona in the system prompt"
```

---

### Task 3: Apply `strip_markdown` to outgoing replies + reword the away-trailer

**Files:**
- Modify: `jarvisd/server.py` (`_send_reply`, `_append_drained_notices`)
- Test: `tests/test_server.py` (add one test)

**Interfaces:**
- Consumes: `strip_markdown` from Task 1.
- Produces: every reply sent to a client (text + TTS source) is markdown-free; the "while you were away" trailer is a spoken sentence.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py  (add this test; FakeAssistant/run_client already exist)
def test_reply_markdown_is_stripped_before_send():
    class MarkdownAssistant(FakeAssistant):
        def process(self, text):
            return "**Sure** — here you go:\n- one\n- two 🎵"

    async def scenario(ws):
        await ws.send(json.dumps({"type": "text_in", "text": "hi"}))
        return json.loads(await ws.recv())

    reply = run_client(JarvisServer(MarkdownAssistant()), scenario)
    assert "**" not in reply["text"]
    assert "- one" not in reply["text"]
    assert "🎵" not in reply["text"]
    assert "Sure" in reply["text"] and "one" in reply["text"] and "two" in reply["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server.py::test_reply_markdown_is_stripped_before_send -q`
Expected: FAIL (`**` still present in `reply["text"]`)

- [ ] **Step 3: Wire in strip_markdown and reword the trailer**

Add the import near the top of `jarvisd/server.py` (with the other imports, after `import websockets`):

```python
from jarvisd.text_format import strip_markdown
```

At the very top of `_send_reply`, clean the reply once (covers both the displayed text and the TTS, which synthesizes from `reply`):

```python
    async def _send_reply(self, ws, reply: str, heard, speak: bool):
        reply = strip_markdown(reply)  # clean prose for both display and TTS
        wav = None
        if speak and self.tts is not None and reply:
            wav = await asyncio.to_thread(self.tts.synthesize, reply)
```

Reword the trailer in `_append_drained_notices` — replace the bulleted block:

```python
        notices = await asyncio.to_thread(self.notifier.drain)
        if notices:
            joined = "; ".join(notices)
            reply = reply + f"\n\nAlso, while you were away: {joined}."
        return reply
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py -q`
Expected: PASS (new test passes; existing server tests still pass — `"echo: hi"` has no markdown so it's unchanged)

- [ ] **Step 5: Commit**

```bash
git add jarvisd/server.py tests/test_server.py
git commit -m "feat: strip markdown from outgoing replies and reword the away-trailer"
```

---

### Task 4: `Assistant.note_interruption()`

**Files:**
- Modify: `jarvisd/core.py` (add module constant near `SYSTEM_PROMPT`, add method to `Assistant`, e.g. after `reset_conversation` around line 350)
- Test: `tests/test_core_memory.py` (add tests)

**Interfaces:**
- Produces: `Assistant.note_interruption() -> None` — appends an interruption marker to the most recent assistant turn in `self.conversation`; no-op if none. `INTERRUPTION_NOTE: str` module constant.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_core_memory.py  (add these tests)
from jarvisd.core import Assistant, INTERRUPTION_NOTE


def _bare_assistant(conversation):
    a = Assistant.__new__(Assistant)  # skip __init__ (no API key / clients needed)
    a.conversation = conversation
    return a


def test_note_interruption_marks_string_assistant_turn():
    a = _bare_assistant([
        {"role": "user", "content": "play music"},
        {"role": "assistant", "content": "I can play lots of things"},
    ])
    a.note_interruption()
    assert a.conversation[-1]["content"].endswith(INTERRUPTION_NOTE)


def test_note_interruption_marks_list_assistant_turn():
    a = _bare_assistant([
        {"role": "assistant", "content": [{"type": "text", "text": "here you go"}]},
    ])
    a.note_interruption()
    last = a.conversation[-1]["content"]
    assert last[-1] == {"type": "text", "text": INTERRUPTION_NOTE}


def test_note_interruption_targets_most_recent_assistant_turn():
    a = _bare_assistant([
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "and then"},
        {"role": "assistant", "content": "second reply"},
    ])
    a.note_interruption()
    assert a.conversation[0]["content"] == "first reply"          # untouched
    assert a.conversation[-1]["content"].endswith(INTERRUPTION_NOTE)


def test_note_interruption_noop_without_assistant_turn():
    a = _bare_assistant([{"role": "user", "content": "hello"}])
    a.note_interruption()  # must not raise
    assert a.conversation == [{"role": "user", "content": "hello"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_core_memory.py -k note_interruption -q`
Expected: FAIL with `ImportError: cannot import name 'INTERRUPTION_NOTE'`

- [ ] **Step 3: Add the constant and method**

Add near the top of `jarvisd/core.py`, just after the `SYSTEM_PROMPT` block:

```python
INTERRUPTION_NOTE = "(The user interrupted and cut off this reply before I finished.)"
```

Add this method to the `Assistant` class (e.g. right after `reset_conversation`):

```python
    def note_interruption(self) -> None:
        """Mark the most recent assistant reply as cut off by the user.

        Called when the user barged in (said the wake word) mid-reply. The
        marker lives in conversation history so the model's next completion
        knows its last reply wasn't fully heard and the user's new message is
        likely a correction or redirect. No-op if there's no assistant turn.
        """
        for turn in reversed(self.conversation):
            if turn["role"] != "assistant":
                continue
            content = turn["content"]
            if isinstance(content, str):
                turn["content"] = content.rstrip() + " " + INTERRUPTION_NOTE
            elif isinstance(content, list):
                content.append({"type": "text", "text": INTERRUPTION_NOTE})
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_core_memory.py -k note_interruption -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add jarvisd/core.py tests/test_core_memory.py
git commit -m "feat: Assistant.note_interruption() marks a cut-off reply in history"
```

---

### Task 5: Server handles the `interrupt` control frame

**Files:**
- Modify: `jarvisd/server.py` (`__init__`, `handle`, `_handle_json`, `_handle_audio`, `_process`)
- Test: `tests/test_server.py` (add tests)

**Interfaces:**
- Consumes: `Assistant.note_interruption()` from Task 4.
- Produces: a `{"type":"interrupt"}` frame sets a per-connection flag; the next transcribed (non-empty) audio turn calls `note_interruption()` before `process()`. `_process(text, note_interruption=False)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server.py  (add these tests)
class NoteAssistant(FakeAssistant):
    def __init__(self):
        super().__init__()
        self.notes = 0

    def note_interruption(self):
        self.notes += 1


def test_interrupt_frame_marks_next_transcribed_turn():
    fake = NoteAssistant()

    async def scenario(ws):
        await ws.send(json.dumps({"type": "interrupt"}))
        await ws.send(np.zeros(16000, dtype=np.int16).tobytes())
        reply = json.loads(await ws.recv())
        if reply.get("audio_follows"):
            await ws.recv()  # drain the WAV frame
        return reply

    reply = run_client(JarvisServer(fake, stt=FakeSTT(), tts=FakeTTS()), scenario)
    assert fake.notes == 1
    assert reply["heard"] == "turn on the light"


def test_interrupt_then_silence_marks_nothing():
    class SilentSTT:
        def transcribe(self, audio):
            return ""

    fake = NoteAssistant()

    async def scenario(ws):
        await ws.send(json.dumps({"type": "interrupt"}))
        await ws.send(np.zeros(1600, dtype=np.int16).tobytes())
        return json.loads(await ws.recv())

    run_client(JarvisServer(fake, stt=SilentSTT(), tts=FakeTTS()), scenario)
    assert fake.notes == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server.py -k interrupt -q`
Expected: FAIL — `fake.notes == 1` is `0` (interrupt frame currently returns an "unknown message type" error and never marks)

- [ ] **Step 3: Implement the interrupt plumbing**

In `__init__`, right after `self._clients: dict = {}` (line 86):

```python
        # Per-connection flag: set by a {"type":"interrupt"} frame, consumed by
        # the next transcribed audio turn to mark the model's last reply cut off.
        self._pending_interrupt: set = set()
```

In `handle`, add the discard to the `finally` cleanup:

```python
        finally:
            self._clients.pop(ws, None)
            self._pending_interrupt.discard(ws)
```

In `_handle_json`, add a branch before the final `else`:

```python
        elif mtype == "interrupt":
            # Client cut off a spoken reply via the wake word. Flag the connection;
            # the next transcribed turn will mark the model's last reply. No reply
            # frame -- the client isn't awaiting one (it's already recording).
            self._pending_interrupt.add(ws)
```

Rewrite `_handle_audio` to read and clear the flag, and pass it through:

```python
    async def _handle_audio(self, ws, pcm: bytes):
        if self.stt is None:
            await self._safe_send(
                ws, json.dumps({"type": "error", "error": "speech not enabled on this server"})
            )
            return
        interrupted = ws in self._pending_interrupt
        self._pending_interrupt.discard(ws)  # consume regardless of outcome
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16)
        log.info("Audio in: %.1fs (%d bytes)", len(audio) / 16000, len(pcm))
        heard = await asyncio.to_thread(self.stt.transcribe, audio)
        if not heard.strip():
            log.info("Transcription empty — no reply generated")
            await self._safe_send(
                ws, json.dumps({"type": "reply", "text": "", "heard": "", "audio_follows": False})
            )
            return
        log.info("Heard: %s", heard)
        reply = await self._process(heard, note_interruption=interrupted)
        reply = await self._append_drained_notices(reply)
        reply = await self._append_habit_trailer(reply)
        await self._send_reply(ws, reply, heard=heard, speak=True)
```

Update `_process` to optionally mark the interruption under the lock:

```python
    async def _process(self, text: str, note_interruption: bool = False) -> str:
        async with self._lock:
            if note_interruption:
                self.assistant.note_interruption()
            return await asyncio.to_thread(self.assistant.process, text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py -q`
Expected: PASS (both new tests pass; existing tests unaffected — the text_in path still calls `_process` with the default `note_interruption=False`)

- [ ] **Step 5: Commit**

```bash
git add jarvisd/server.py tests/test_server.py
git commit -m "feat: server marks interruption on the next turn after an interrupt frame"
```

---

### Task 6: `_wav_duration()` in the voice client

**Files:**
- Modify: `clients/voice_client.py` (imports + new static method)
- Test: `tests/test_voice_client.py` (add tests)

**Interfaces:**
- Produces: `VoiceClient._wav_duration(wav: bytes) -> float` — seconds of a WAV clip from its header; `0.0` if unreadable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice_client.py  (add these tests)
import io
import wave

import numpy as np


def _make_wav(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.zeros(int(seconds * rate), dtype=np.int16).tobytes())
    return buf.getvalue()


def test_wav_duration_reads_header():
    assert abs(VoiceClient._wav_duration(_make_wav(0.5)) - 0.5) < 0.01
    assert abs(VoiceClient._wav_duration(_make_wav(1.0, rate=22050)) - 1.0) < 0.01


def test_wav_duration_bad_bytes_is_zero():
    assert VoiceClient._wav_duration(b"not a wav") == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_voice_client.py -k wav_duration -q`
Expected: FAIL with `AttributeError: type object 'VoiceClient' has no attribute '_wav_duration'`

- [ ] **Step 3: Add imports and the method**

Add to the imports block at the top of `clients/voice_client.py` (after `import os`):

```python
import io
import wave
```

Add this static method to `VoiceClient` (e.g. just after `_failure_beep`):

```python
    @staticmethod
    def _wav_duration(wav: bytes) -> float:
        """Length of a WAV clip in seconds from its header (0.0 if unreadable)."""
        try:
            with wave.open(io.BytesIO(wav), "rb") as w:
                rate = w.getframerate()
                return w.getnframes() / float(rate) if rate else 0.0
        except (wave.Error, EOFError):
            return 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_voice_client.py -k wav_duration -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add clients/voice_client.py tests/test_voice_client.py
git commit -m "feat: _wav_duration() parses a reply clip's length for barge-in timing"
```

---

### Task 7: Extract `_record_utterance()` from `_capture_utterance`

**Files:**
- Modify: `clients/voice_client.py` (`_capture_utterance`, new `_record_utterance`)
- Test: `tests/test_voice_client.py` (add tests)

**Interfaces:**
- Produces: `VoiceClient._record_utterance() -> bytes | None` — beeps, records until silence (no wake word), returns PCM bytes or `None`. `_capture_utterance` now waits for the wake word then delegates to it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice_client.py  (add these tests; _client/FakeMic/FakeVAD already exist)
def test_record_utterance_returns_pcm_on_speech():
    audio = np.ones(16000, dtype=np.int16)
    c = _client(audio)
    c._wake_buf = np.array([], dtype=np.int16)
    out = c._record_utterance()
    assert out == audio.tobytes()
    assert c.mic.clears == 1


def test_record_utterance_returns_none_on_silence():
    c = _client(None)
    c._wake_buf = np.array([], dtype=np.int16)
    assert c._record_utterance() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_voice_client.py -k record_utterance -q`
Expected: FAIL with `AttributeError: 'VoiceClient' object has no attribute '_record_utterance'`

- [ ] **Step 3: Add `_record_utterance` and slim down `_capture_utterance`**

Add the new method (place it just before `_capture_followup`):

```python
    def _record_utterance(self) -> bytes | None:
        """Record one utterance NOW (no wake word): beep, capture until silence.

        Returns PCM bytes, or None if nothing was said. Used both right after a
        wake-word detection and right after a barge-in interrupt.
        """
        self.mic.clear()  # drop the wake phrase / anything said while JARVIS talked
        self._wake_buf = np.array([], dtype=np.int16)
        try:
            winsound.Beep(800, 200)
        except Exception:
            pass
        print("  Listening...")
        audio = self.vad.record_until_silence(
            self.mic, max_seconds=15.0, silence_ms=800, speech_timeout_sec=5.0,
        )
        if audio is None or len(audio) < 1600:
            log.info("No speech captured")
            print("  (no speech detected)\n")
            self._failure_beep()
            return None
        log.info("Captured %.1fs of audio", len(audio) / 16000)
        return audio.tobytes()
```

Replace the body of `_capture_utterance` (the wake-word loop) so it delegates to `_record_utterance` after detection:

```python
    def _capture_utterance(self) -> bytes | None:
        """Block until the wake word, then record one utterance. PCM or None."""
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
            return self._record_utterance()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_voice_client.py -q`
Expected: PASS (all voice-client tests, including the existing follow-up + state-machine ones)

- [ ] **Step 5: Commit**

```bash
git add clients/voice_client.py tests/test_voice_client.py
git commit -m "refactor: extract _record_utterance() shared by wake and barge-in paths"
```

---

### Task 8: `_watch_for_wake()` + `_play_and_watch()` interruptible playback

**Files:**
- Modify: `clients/voice_client.py` (imports + three methods)
- Test: `tests/test_voice_client.py` (add tests for `_watch_for_wake`)

**Interfaces:**
- Consumes: `_wav_duration` (Task 6), the existing `WakeWordDetector` (`self.wake_word.process_frame`), `self.mic`, `self._play_lock`, `WAKE_WORD_CHUNK`.
- Produces:
  - `VoiceClient._watch_for_wake(duration: float) -> bool` — reads the mic for up to `duration`(+tail) seconds, returns `True` as soon as the wake word fires, else `False`.
  - `VoiceClient._play_and_watch(wav: bytes) -> bool` — async-plays `wav` while watching; returns `True` if interrupted.
  - `VoiceClient._play_and_watch_reply(wav: bytes) -> bool` — async wrapper holding `_play_lock`, runs `_play_and_watch` in a thread.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_voice_client.py  (add these tests)
import time

WAKE_CHUNK = 1280  # WAKE_WORD_CHUNK


class FrameMic:
    """Mic that always has a full wake-word chunk ready."""
    def __init__(self):
        self.clears = 0

    def clear(self):
        self.clears += 1

    def read_frame(self, timeout=1.0):
        return np.zeros(WAKE_CHUNK, dtype=np.int16)


class FakeWake:
    """process_frame returns True on the Nth call (0 = never)."""
    def __init__(self, fire_on=0):
        self.fire_on = fire_on
        self.calls = 0

    def process_frame(self, chunk):
        self.calls += 1
        return self.fire_on and self.calls >= self.fire_on


def _watch_client(wake):
    c = VoiceClient.__new__(VoiceClient)
    c.mic = FrameMic()
    c.wake_word = wake
    c._wake_buf = np.array([], dtype=np.int16)
    return c


def test_watch_for_wake_returns_true_when_word_fires():
    c = _watch_client(FakeWake(fire_on=2))
    assert c._watch_for_wake(duration=5.0) is True
    assert c.mic.clears == 1


def test_watch_for_wake_returns_false_after_duration():
    c = _watch_client(FakeWake(fire_on=0))  # never fires
    start = time.monotonic()
    assert c._watch_for_wake(duration=0.05) is False
    assert time.monotonic() - start < 2.0  # exits near the deadline, doesn't hang
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_voice_client.py -k watch_for_wake -q`
Expected: FAIL with `AttributeError: 'VoiceClient' object has no attribute '_watch_for_wake'`

- [ ] **Step 3: Add the import and the three methods**

Add to the imports block at the top of `clients/voice_client.py` (after `import os`):

```python
import time
```

Add these methods to `VoiceClient` (place them after `_record_utterance`):

```python
    def _watch_for_wake(self, duration: float) -> bool:
        """While a reply plays, listen for the wake word. Returns True on a
        barge-in, False if `duration` (+ a short tail) elapses with no wake word.

        Echo-safe: the mic hears JARVIS's own TTS, but the wake-word model only
        trips on 'hey jarvis' -- which JARVIS never says -- so it can't self-fire.
        """
        self.mic.clear()
        self._wake_buf = np.array([], dtype=np.int16)
        deadline = time.monotonic() + duration + 0.3  # small tail past clip end
        while time.monotonic() < deadline:
            frame = self.mic.read_frame(timeout=0.2)
            if frame is None:
                continue
            self._wake_buf = np.concatenate([self._wake_buf, frame])
            while len(self._wake_buf) >= WAKE_WORD_CHUNK:
                chunk = self._wake_buf[:WAKE_WORD_CHUNK]
                self._wake_buf = self._wake_buf[WAKE_WORD_CHUNK:]
                if self.wake_word.process_frame(chunk):
                    return True
        return False

    def _play_and_watch(self, wav: bytes) -> bool:
        """Play a reply WAV asynchronously while watching for the wake word.
        Returns True if the user interrupted, False if it finished on its own.
        """
        duration = self._wav_duration(wav)
        self._play_buf = wav  # hold a reference alive for async playback
        try:
            winsound.PlaySound(wav, winsound.SND_MEMORY | winsound.SND_ASYNC)
        except Exception:
            # Async playback unavailable: fall back to a plain blocking play
            # (no barge-in this time, but the reply is still heard).
            try:
                winsound.PlaySound(wav, winsound.SND_MEMORY)
            except Exception:
                pass
            return False
        try:
            return self._watch_for_wake(duration)
        finally:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)  # stop if still playing
            except Exception:
                pass

    async def _play_and_watch_reply(self, wav: bytes) -> bool:
        """Play a reply under the shared play-lock; return whether interrupted."""
        async with self._play_lock:
            return await asyncio.to_thread(self._play_and_watch, wav)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_voice_client.py -k watch_for_wake -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add clients/voice_client.py tests/test_voice_client.py
git commit -m "feat: interruptible reply playback that watches for the wake word"
```

---

### Task 9: Wire barge-in into the run loop

**Files:**
- Modify: `clients/voice_client.py` (imports + the `run()` capture/playback loop)
- Test: `tests/test_voice_client.py` (full suite regression) + manual live checklist

**Interfaces:**
- Consumes: `_play_and_watch_reply` (Task 8), `_record_utterance` (Task 7), existing `_capture_followup`/`_capture_utterance`/`_followup_next`/`advance`.
- Produces: on a barge-in, the client sends `{"type":"interrupt"}` then records the user's new utterance and continues the conversation.

- [ ] **Step 1: Add the `json` import**

Add to the imports block at the top of `clients/voice_client.py` (after `import io`):

```python
import json
```

- [ ] **Step 2: Replace the run-loop body**

Replace the `while True:` loop inside `run()` (from `in_conversation = False` down to the end of the `advance(productive=True)` handling — the block currently ending just before `finally:`) with this. It adds a `pending_barge_in` state and interruptible reply playback:

```python
                in_conversation = False  # True during a hands-free back-and-forth
                misfires = 0             # consecutive non-productive follow-ups
                pending_barge_in = False  # user interrupted; record their new turn next

                def advance(productive: bool):
                    """Apply the follow-up state transition; announce an ending."""
                    nonlocal in_conversation, misfires
                    was = in_conversation
                    in_conversation, misfires = self._followup_next(
                        in_conversation=in_conversation, misfires=misfires,
                        productive=productive,
                    )
                    if was and not in_conversation:
                        print("  (conversation ended — say 'hey jarvis' to start again)\n")

                try:
                    while True:
                        if pending_barge_in:
                            # We just cut off a reply; the user is already talking.
                            pending_barge_in = False
                            pcm = await asyncio.to_thread(self._record_utterance)
                            if pcm is None:
                                advance(productive=False)
                                continue
                        elif in_conversation:
                            print("  Listening for follow-up... (or stay quiet to end)")
                            pcm = await asyncio.to_thread(self._capture_followup)
                            if pcm is None:
                                misfires = MAX_FOLLOWUP_MISFIRES  # silent window ends it
                                advance(productive=False)
                                continue
                        else:
                            pcm = await asyncio.to_thread(self._capture_utterance)
                            if pcm is None:
                                continue
                        await ws.send(pcm)
                        item = await receiver.get_reply(recv_task)
                        reply = item["data"]
                        if reply.get("type") == "error":
                            print(f"  [error] {reply.get('error')}\n")
                            self._failure_beep()
                            misfires = MAX_FOLLOWUP_MISFIRES
                            advance(productive=False)
                            continue
                        if not reply.get("heard") and not reply.get("text"):
                            print("  (didn't catch that)\n")
                            self._failure_beep()
                            advance(productive=False)
                            continue
                        if reply.get("heard"):
                            print(f"  You: {reply['heard']}")
                        if reply.get("text"):
                            print(f"  JARVIS: {reply['text']}\n")
                        if reply.get("audio_follows"):
                            wav = await item["wav_future"]
                            if wav is not None:
                                interrupted = await self._play_and_watch_reply(wav)
                                if interrupted:
                                    print("  [interrupted]")
                                    await ws.send(json.dumps({"type": "interrupt"}))
                                    pending_barge_in = True
                                    continue  # skip advance; go record the new turn
                        # A completed exchange -> keep listening for a follow-up.
                        advance(productive=True)
```

- [ ] **Step 3: Run the full suite to verify no regressions**

Run: `python -m pytest -q`
Expected: PASS (all tests green — the loop change is exercised indirectly; the state helpers and playback pieces are unit-tested in Tasks 6–8)

- [ ] **Step 4: Manual live verification (two terminals)**

Start the daemon (`python -m jarvisd`, wait for `jarvisd listening`), then the client (`python -m clients.voice_client`), and confirm:
1. Say "hey jarvis", ask something that yields a long reply, then say "hey jarvis" **while it's talking** → audio stops within ~0.3s, `[interrupted]` prints, it beeps and listens.
2. Give a correction ("no, play Tamil songs") → it acts on the correction and doesn't repeat the cut-off reply (interrupt-awareness working end-to-end).
3. Let a reply finish normally → the hands-free follow-up flow still works, and going quiet still ends the conversation.
4. Replies sound like spoken prose — no "asterisk asterisk", no bullet points read aloud.

- [ ] **Step 5: Commit**

```bash
git add clients/voice_client.py
git commit -m "feat: barge-in — wake word interrupts a spoken reply and captures the correction"
```

---

## Self-Review

**Spec coverage:**
- Tone Layer 1 (persona/style prompt) → Task 2. ✅
- Tone Layer 2 (`strip_markdown` on outgoing reply) → Task 1 (fn) + Task 3 (wiring). ✅
- Tone Layer 3 (reworded away-trailer) → Task 3. ✅
- Barge-in `_play_and_watch` + `_wav_duration` + echo-safety → Tasks 6, 8. ✅
- `_record_utterance` extraction + straight-to-record after interrupt → Task 7 + Task 9. ✅
- Run-loop integration + `{"type":"interrupt"}` frame → Task 9. ✅
- Interrupt-awareness: `note_interruption()` → Task 4; server flag + control frame + `_process` flag → Task 5; persona reinforcement line → Task 2 (persona bullet "If your previous reply was cut off…"). ✅
- Testing: `strip_markdown` (T1), persona assertion (T2), server strip (T3), `note_interruption` (T4), server interrupt (T5), `_wav_duration` (T6), `_record_utterance` (T7), `_watch_for_wake` (T8), full-suite + live (T9). ✅
- Scope boundaries (Spotify music / proactive pushes untouched): proactive audio still uses the blocking `_play`; only reply playback in `run()` becomes interruptible (Task 9). ✅

**Placeholder scan:** No TBD/TODO; every code step carries complete code and exact commands. ✅

**Type consistency:** `strip_markdown(str)->str` (T1) used in T3. `note_interruption()->None` + `INTERRUPTION_NOTE` (T4) used in T5's `_process`. `_wav_duration(bytes)->float` (T6) used by `_play_and_watch` (T8). `_record_utterance()->bytes|None` (T7) used in T9. `_watch_for_wake(float)->bool`, `_play_and_watch(bytes)->bool`, `_play_and_watch_reply(bytes)->bool` (T8) used in T9. `_followup_next`/`advance`/`MAX_FOLLOWUP_MISFIRES` reused unchanged. ✅
