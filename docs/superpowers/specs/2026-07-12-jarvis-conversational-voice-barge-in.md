# JARVIS — Conversational Voice + Barge-In: Design

**Date:** 2026-07-12
**Status:** Approved by user (conversation review, all sections)
**Project:** `C:\Users\akshi\Projects\personal-assistant`

## Goal

Make talking to JARVIS feel like talking to a person, in two ways the user
called out during live voice testing:

1. **Barge-in** — interrupt JARVIS mid-reply (say the wake word) to cut it off
   and immediately ask something new or correct a misheard request, instead of
   waiting out a long spiel.
2. **Conversational tone** — replies currently read like a chatbot: markdown
   bullet lists, bold, emoji, "You can ask for:" scaffolding — and that raw
   markup is spoken verbatim by the TTS. Give JARVIS a concise, dry, butler-ish
   persona and guarantee the spoken output is clean prose.

Non-goals: no new models or routing changes (Haiku handles the persona when
instructed well); no acoustic echo cancellation; no changes to Spotify music
playback or proactive-push playback.

## Decisions from brainstorming

| Question | Decision |
|---|---|
| Tone/register | **JARVIS persona** — concise, dry wit, quiet competence, familiar |
| How JARVIS addresses the user | **No honorific**, no name — talks to them directly |
| How to interrupt while speaking | **Say the wake word** ("hey jarvis") — robust against echo, hands-free |
| Interrupt-awareness to the model | **Yes, in v1** — the model is told when its previous reply was cut off |
| Barge-in scope | JARVIS's **spoken replies** in the main loop only (not Spotify music, not proactive pushes) |

---

## 1. Conversational tone

The AI-brochure feel has two independent causes, fixed in three layers.

### Layer 1 — Persona + hard style rules (system prompt)

Replace the current vague guideline ("Be conversational but concise") in
`SYSTEM_PROMPT` (`jarvisd/core.py`) with a persona block:

- **Who JARVIS is:** dry, understated wit; quietly competent; familiar with the
  user; addresses them directly (no "sir", no name-dropping).
- **Hard output rules (the load-bearing part):**
  - Speak in natural spoken sentences only. **No markdown, no bullet or
    numbered lists, no headers, no emoji, no bold/italic markup.**
  - Default to **one or two sentences.** Expand only when the user asks for
    detail or a genuine enumeration is unavoidable — and even then, speak it
    ("You've got three: X, Y, and Z"), don't format it.
  - Use contractions.
  - When clarifying, offer the **single most likely interpretation as a
    question** ("Did you mean Tamil songs?") rather than listing five
    candidates.
  - If your previous reply was cut off, the user is correcting or redirecting
    you — adjust, don't repeat yourself. (Ties into §2 interrupt-awareness.)

### Layer 2 — `strip_markdown()` safety net

Even well-prompted, a model occasionally emits `**bold**` or a `- list`, and
today that markup is what gets spoken. Add a small **pure** function
(`jarvisd/text_format.py`, `strip_markdown(text) -> str`) that:

- removes bold/italic markers (`**`, `__`, `*`, `_`), inline/backtick code
  fences, and heading markers (`#`);
- turns stray `- ` / `* ` / `1. ` list-item lines into plain sentences
  (strip the marker; ensure each ends with sentence punctuation so the TTS
  pauses naturally);
- drops emoji.

Applied to the **outgoing reply string** the server sends (so both the terminal
text and the synthesized TTS use the clean version). It operates only on the
output copy — it does **not** alter `assistant.conversation`, the history the
model reasons over. Point of application: in `jarvisd/server.py`, on the reply
just before `_send_reply` (after notices/habit trailer are appended, so those
are cleaned too).

### Layer 3 — Reword the "While you were away" trailer

`_append_drained_notices` currently produces a bulleted "While you were away:"
block that gets read aloud. Reword to spoken prose, e.g.
*"Oh — while you were away, your stretch reminder went off."* (single sentence;
multiple notices joined naturally). Keeps the whole spoken reply human.

### Testing (tone)

- `strip_markdown()`: characterization tests — bold/italic/headers/backticks
  removed; `- ` bullets become sentences; emoji dropped; already-clean prose
  passes through unchanged; multi-line list collapses to speakable text.
- A light assertion that `SYSTEM_PROMPT` carries the no-markdown/no-list rule
  (guards against accidental regression of the persona block).
- Persona *quality* is not unit-testable — validated by the user in live voice.

---

## 2. Barge-in (say "hey jarvis" to interrupt)

### The problem today

`VoiceClient._play()` calls **blocking** `winsound.PlaySound(...)`. While JARVIS
talks, the mic is unwatched and the audio can't be stopped. Fix = play
asynchronously while a watcher listens for the wake word, and cut the audio the
moment it fires.

### Core mechanism — `_play_and_watch(wav) -> bool`

Returns "was I interrupted?". Runs inside `asyncio.to_thread` under the existing
`_play_lock` (so it still serializes with proactive audio).

1. Start playback non-blocking: `winsound.PlaySound(wav, SND_MEMORY | SND_ASYNC)`,
   holding a reference to the buffer so it stays valid for the duration.
2. Compute the clip length from the WAV header via `_wav_duration(wav) -> float`
   (a tiny pure, testable helper) to know when playback ends naturally.
3. Loop in ~80ms wake-word chunks reading the mic and running the existing
   `WakeWordDetector`, until **either**:
   - the wake word fires → `winsound.PlaySound(None, SND_PURGE)` stops it
     instantly → return `True`; **or**
   - the computed duration (plus a small margin) elapses → return `False`.

**Why it's echo-safe:** the mic hears JARVIS's own TTS from the speakers, but
the wake-word model only trips on "hey jarvis" — which JARVIS never says. Its
own voice cannot self-interrupt. (This is why wake-word beat "just start
talking".)

### Flow after an interrupt

The user already spoke the wake word to interrupt, so we go **straight to
recording** — no second "hey jarvis". Extract the record-after-wake half of
`_capture_utterance` into a shared `_record_utterance() -> bytes | None` used by
both the normal wake path and the post-interrupt path. The recorded utterance
becomes the next turn and the hands-free conversation continues. If the user
interrupts but then says nothing intelligible, it falls back to the normal
follow-up / wake-word gating (no special state left dangling).

### Run-loop integration (`VoiceClient.run`)

Where the loop currently does `await self._play(wav)` for a reply, call
`interrupted = await self._play_and_watch(wav)`. If `interrupted`: (1) send the
`{"type": "interrupt"}` control frame immediately (the client already knows it
was cut off — see §2 interrupt-awareness), (2) record the next utterance
directly via `_record_utterance`, (3) send that PCM and continue the
conversation. Proactive audio keeps calling the simple blocking `_play`.

### Interrupt-awareness to the model

The model's memory is `assistant.conversation`; the audio channel is raw PCM
with no room for a flag. So:

- **Client → server signal.** On barge-in, right after cutting the audio, the
  client sends a control frame `{"type": "interrupt"}`, *then* records and sends
  the new utterance. Ordering is guaranteed: the client sends nothing else in
  between, and the server processes a connection's messages in receive order.
- **Server.** Each connection carries a `pending_interrupt` flag. The `interrupt`
  control message sets it. On the next transcribed utterance that actually
  yields text, the server calls `assistant.note_interruption()` **before**
  `process()`, then clears the flag. If the interrupting utterance transcribes
  empty, nothing is marked and the flag is cleared (no stale state).
- **`assistant.note_interruption()`.** Finds the most recent assistant turn in
  history and appends a short marker to it — e.g.
  *"…(the user cut this reply off before I finished)"*. Handles both plain-text
  and tool-use (list) turn shapes; no-ops if there is no prior assistant turn.
  Because the marker lives in history, the model's next completion sees that its
  last reply wasn't fully delivered, and — per the Layer 1 rule — treats the new
  input as a correction/redirect.

### Scope boundaries

- **Spotify music** isn't played by the client, so "interrupt during music" is
  already just the normal wake-word flow — unchanged.
- **Proactive pushes** (usually one line) keep the simple blocking `_play`.

### Testing (barge-in)

- `_wav_duration()`: pure test — a known WAV byte buffer yields the correct
  seconds; handles a couple of sample rates.
- `_play_and_watch()` frame loop: with a fake mic (canned frames) and a fake
  wake detector, assert it returns `True` and stops early when the detector
  fires, and `False` when the duration elapses with no trigger. Playback
  start/stop is injectable/mocked so tests don't hit `winsound`.
- `_record_utterance()`: reuse the existing `_capture_followup`-style fakes
  (fake mic + fake VAD) to assert PCM bytes on speech, `None` on silence.
- `note_interruption()`: marker appended to the last assistant turn; no-op with
  no assistant turn; correct handling of a tool-use-shaped (list-content) turn.
- Server: an `interrupt` control frame sets the per-connection flag, and the
  next audio turn invokes `note_interruption()` (via fake assistant/STT,
  following existing `tests/test_server.py` patterns). An empty follow-up clears
  the flag without marking.

---

## Files touched

- `jarvisd/core.py` — `SYSTEM_PROMPT` persona rewrite; `Assistant.note_interruption()`.
- `jarvisd/text_format.py` *(new)* — `strip_markdown()`.
- `jarvisd/server.py` — apply `strip_markdown` to outgoing reply; per-connection
  `pending_interrupt`; handle `{"type":"interrupt"}`; reword notices trailer.
- `clients/voice_client.py` — `_play_and_watch`, `_wav_duration`,
  `_record_utterance`; run-loop integration; send `interrupt` control frame.
- `tests/` — `test_text_format.py` *(new)*, additions to `test_core*.py`,
  `test_server.py`, `test_voice_client.py`.

## Security / invariants preserved

- No change to the security guard, audit choke point, or path/URL/keyword blocks.
- `strip_markdown` touches only the outbound reply copy, never conversation
  history or stored memory.
- The WebSocket server stays loopback-bound and unauthenticated by design; the
  new `interrupt` control frame carries no data and no new trust boundary.
- Interrupt-awareness only annotates history text; it does not bypass any turn
  boundary or destructive-confirm logic.
