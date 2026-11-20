# JARVIS Turn Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS correct spoken-turn boundaries — more patience at the start of a turn, an immediate close when nothing is expected back, and spoken phrases that end or void a turn.

**Architecture:** Turn policy stays on the server. A new pure classifier in `voice/stt.py` labels each transcript; a new per-turn flag on `Assistant` carries the destructive-action confirmation verdict out of the agent loop; the server combines the two into one additive `"turn"` field on the reply JSON, which the thin client obeys. The client gains no new recognition logic — it stays dumb by design, because transcription is server-side.

**Tech Stack:** Python 3.11, pytest, numpy, faster-whisper, webrtcvad, websockets, pystray, edge-tts, pyttsx3

**Spec:** `docs/superpowers/specs/2026-11-20-jarvis-turn-boundaries-design.md`

## Global Constraints

- Base branch: `master` @ `fc05523`, level with `origin/master`. Work on a feature branch.
- Pre-speech timeout (time to start talking, post-wake-word): **10.0s** (was 5.0s)
- Trailing silence, post-wake-word capture: **1200ms** (was 800ms)
- Trailing silence, follow-up capture: **800ms** (unchanged, now explicit)
- Silence trim before verification: keep **150ms** of trailing silence, amplitude threshold **300** (matches `clients/enroll_voice.py:187`)
- Edge TTS per-attempt budget: **5.0s**, **2** attempts total, then SAPI fallback
- All new reply-JSON fields are **additive**. A client that ignores them must behave exactly as today (precedent: `reason`, `server.py:334-341`).
- Phrase lists are stored **pre-normalised** against `re.sub(r"[^a-z0-9 ]+", " ", text.lower())` — i.e. `"that s it"`, not `"that's it"`.
- A spoken "yes"/"no" confirmation must survive every change (`MIN_SPEECH_MS` and `_FILLER_UTTERANCES` comments explain why: ~200-300ms of audio gates a destructive tool call).
- Pure policy logic must be unit-testable without a model, audio, or network, following `_is_hallucination` and `SpeakerVerifier.decide`.

## Review Focus

1. **A concluder spoken as the answer to a confirmation prompt** ("delete the 3pm meeting?" → "that's it") must NOT close the mic or swallow the answer — Task 5.
2. **"never mind" as the very first utterance after the wake word**, where `in_conversation` is still `False`, must reopen the window rather than drop to idle — Task 6.
3. **A reply genuinely ending in "?" while a drained notice or habit trailer is pending** must still hold the mic open; the flag is computed before those append — Task 5.
4. **A phrase written with an apostrophe** must still match after normalisation, or the whole feature silently never fires — Task 3.
5. **A briskly spoken "Yes." (~200-300ms)** must survive the silence trim and still verify — Task 1.

---

### Task 1: Trim trailing silence before speaker verification

**Files:**
- Modify: `voice/audio_io.py` (add module-level function)
- Modify: `jarvisd/server.py:331-343` (trim before `speaker_verifier.accepts`)
- Modify: `voice/speaker_verify.py:65` and the comment block above `DEFAULT_MIN_DURATION_SEC` (they assert the 800ms assumption as fact)
- Test: `tests/test_audio_io.py`, `tests/test_server_verify_trim.py` (create)

**Interfaces:**
- Consumes: nothing
- Produces: `trim_trailing_silence(audio: np.ndarray, *, threshold: int = 300, keep_ms: int = 150, sample_rate: int = SAMPLE_RATE) -> np.ndarray` in `voice/audio_io.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_trims_trailing_silence_but_keeps_a_margin():
    speech = (np.ones(16000, dtype=np.int16) * 1000)       # 1.0s loud
    silence = np.zeros(16000, dtype=np.int16)              # 1.0s silent
    out = trim_trailing_silence(np.concatenate([speech, silence]))
    assert 16000 + 2000 <= len(out) <= 16000 + 2800        # ~150ms kept (+/- a frame)

def test_keeps_a_short_yes_intact():
    """A briskly spoken 'Yes.' is ~200-300ms; the trim must not eat it."""
    yes = (np.ones(4000, dtype=np.int16) * 1000)           # 250ms
    out = trim_trailing_silence(np.concatenate([yes, np.zeros(16000, dtype=np.int16)]))
    assert len(out) >= 4000

def test_leading_silence_is_untouched():
    audio = np.concatenate([np.zeros(8000, dtype=np.int16),
                            (np.ones(8000, dtype=np.int16) * 1000)])
    assert len(trim_trailing_silence(audio)) == len(audio)

def test_all_silence_returns_the_clip_unchanged():
    """No speech to anchor on: return the input rather than an empty array,
    so the verifier still sees a clip and rejects it on its own terms."""
    audio = np.zeros(16000, dtype=np.int16)
    assert len(trim_trailing_silence(audio)) == len(audio)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_audio_io.py -k trim -v`
Expected: FAIL with `NameError`/`ImportError` on `trim_trailing_silence`

- [ ] **Step 3: Implement `trim_trailing_silence` in `voice/audio_io.py`**

Scan backwards for the last sample whose absolute value exceeds `threshold`, then keep `keep_ms` beyond it. Return the input unchanged when no sample exceeds the threshold.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audio_io.py -k trim -v`
Expected: PASS

- [ ] **Step 5: Write the failing server-wiring test**

```python
@pytest.mark.asyncio
async def test_verifier_sees_trimmed_audio_and_stt_sees_the_full_clip():
    """The trim protects the verification score; Whisper keeps the trailing
    context and its own confidence gates handle the silence."""
    seen = {}
    verifier = FakeVerifier(on_accepts=lambda a: seen.__setitem__("verify_len", len(a)))
    stt = FakeStt(on_transcribe=lambda a: seen.__setitem__("stt_len", len(a)))
    # 1.0s of speech + 1.0s of silence
    await server._handle_audio(FakeWs(), pcm_of(speech_ms=1000, silence_ms=1000))
    assert seen["verify_len"] < seen["stt_len"]
    assert seen["stt_len"] == 32000
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m pytest tests/test_server_verify_trim.py -v`
Expected: FAIL — `verify_len == stt_len`

- [ ] **Step 7: Wire the trim into `_handle_audio`**

In `jarvisd/server.py:331-343`, pass `trim_trailing_silence(audio)` to `self.speaker_verifier.accepts`, leaving the `audio` passed to `self.stt.transcribe` untouched.

- [ ] **Step 8: Update the stale comments**

`voice/speaker_verify.py:65` states "*captures close after 800ms of trailing silence, nearly every real command is 1.0-1.8s*" as fact. Rewrite it to describe the trim as the reason short clips remain the common path, and update `tests/test_speaker_verify.py:223` if its reasoning cites the 800ms figure.

- [ ] **Step 9: Check the enrollment and calibration paths for consistency**

`clients/enroll_voice.py:74` and `scripts/voice_calibrate.py:228` hardcode `silence_ms=800` and feed the voiceprint and the measured score bands. They are **not** being changed, but the clips they embed must have the same shape as the clips now being judged, or the calibrated numbers in `data/verify_config.json` describe something else. Confirm whether the trim must also be applied where those build their embeddings; if so, apply it there in this task. Record the finding either way in the commit body — a silent mismatch here is the Anti-goal "a real command silently rejected as unverified".

- [ ] **Step 10: Run the full suite and commit**

Run: `python -m pytest -q`
Expected: PASS

```bash
git add voice/audio_io.py jarvisd/server.py voice/speaker_verify.py tests/
git commit -m "voice: trim trailing silence before speaker verification"
```

---

### Task 2: Timing — more patience at the start of a turn

**Files:**
- Modify: `clients/voice_client.py` (new module constants near `FOLLOWUP_WINDOW_SEC:38`; `_record_utterance:327`; `_capture_followup:485`)
- Test: `tests/test_voice_client.py:84-91` (rewrite the pinned-kwargs test)

**Interfaces:**
- Consumes: nothing
- Produces: module constants `UTTERANCE_SPEECH_TIMEOUT_SEC` (10.0), `UTTERANCE_SILENCE_MS` (1200), `FOLLOWUP_SILENCE_MS` (800) in `clients/voice_client.py`, each read from a same-named env var

- [ ] **Step 1: Rewrite the failing test**

`tests/test_voice_client.py:84-91` currently asserts both callers pass identical kwargs, under a test named `..._uses_the_same_vad_params_as_capture_utterance`. That invariant is being deliberately broken. Replace it with:

```python
def test_wake_word_capture_is_patient():
    kwargs = captured_vad_kwargs(client._record_utterance)
    assert kwargs["speech_timeout_sec"] == 10.0
    assert kwargs["silence_ms"] == 1200

def test_followup_capture_stays_tight():
    """Once a completed turn closes the mic, the only turns reaching the
    follow-up capture are confirmations and answers to direct questions,
    where a tight endpoint is correct."""
    kwargs = captured_vad_kwargs(client._capture_followup)
    assert kwargs["silence_ms"] == 800
    assert kwargs["speech_timeout_sec"] == FOLLOWUP_WINDOW_SEC
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_voice_client.py -k "patient or stays_tight" -v`
Expected: FAIL — `speech_timeout_sec == 5.0`, `silence_ms == 800`

- [ ] **Step 3: Add the constants and use them**

Define the three constants beside `FOLLOWUP_WINDOW_SEC` following its exact `float(os.getenv(...))` / `int(os.getenv(...))` pattern, then replace the literals in the `record_until_silence` calls at `_record_utterance:327` and `_capture_followup:485`. Leave `max_seconds=15.0` alone.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_voice_client.py -v`
Expected: PASS

- [ ] **Step 5: Document and commit**

Add the three variables to `.env.example` as commented defaults, following the existing style there.

```bash
git add clients/voice_client.py tests/test_voice_client.py .env.example
git commit -m "voice: configurable capture timing, more patience after the wake word"
```

---

### Task 3: Classify a transcript as concluder, void, noise or normal

**Files:**
- Modify: `voice/stt.py`
- Test: `tests/test_stt_turn_intent.py` (create)

**Interfaces:**
- Consumes: nothing
- Produces, in `voice/stt.py`:
  - `INTENT_NORMAL = "normal"`, `INTENT_CONCLUDE = "conclude"`, `INTENT_VOID_CONTINUE = "void_continue"`, `INTENT_VOID_CLOSE = "void_close"`, `INTENT_NOISE = "noise"`
  - `classify_text(text: str) -> str` — pure, no model, no audio
  - `SpeechToText.transcribe_classified(self, audio: np.ndarray) -> tuple[str, str]` returning `(text, intent)`
  - `SpeechToText.transcribe` keeps its signature and returns `""` for any intent other than `INTENT_NORMAL`

- [ ] **Step 1: Write the failing tests**

```python
# Concluders: the phrase is the WHOLE utterance.
@pytest.mark.parametrize("text", [
    "That's it.", "that's all", "Thank you.", "thanks", "We're done.", "thats it",
])
def test_whole_utterance_concluders(text):
    assert classify_text(text) == INTENT_CONCLUDE

# Void phrases: anchored to the END of the utterance.
def test_void_continue_at_end_of_a_real_command():
    assert classify_text("remind me at five, never mind") == INTENT_VOID_CONTINUE

@pytest.mark.parametrize("text", ["scratch that", "disregard that", "Never mind."])
def test_void_continue_phrases(text):
    assert classify_text(text) == INTENT_VOID_CONTINUE

def test_void_close_closes_the_mic():
    assert classify_text("remind me at five, forget it") == INTENT_VOID_CLOSE

# The safety property: anchoring to the end.
def test_a_void_phrase_at_the_START_is_a_normal_command():
    assert classify_text("never mind that, what's the weather") == INTENT_NORMAL

def test_a_concluder_inside_a_real_command_is_normal():
    """The trap that killed trailing concluders: an ends-with rule would
    strip this one and leave 'remind me to say'."""
    assert classify_text("remind me to say thank you") == INTENT_NORMAL

def test_noise_is_still_noise():
    assert classify_text("see you in the next video") == INTENT_NOISE
    assert classify_text("hmm") == INTENT_NOISE

def test_a_spoken_confirmation_is_never_a_concluder():
    for text in ("yes", "no", "okay", "yeah", "sure"):
        assert classify_text(text) == INTENT_NORMAL

def test_transcribe_returns_text_only_for_normal_intent(monkeypatch):
    stt = stub_stt(returns="that's it")
    assert stt.transcribe(AUDIO) == ""
    assert stt.transcribe_classified(AUDIO) == ("that's it", INTENT_CONCLUDE)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stt_turn_intent.py -v`
Expected: FAIL with `ImportError` on `classify_text`

- [ ] **Step 3: Implement the phrase sets and `classify_text`**

Store all three phrase sets **pre-normalised** against the existing
`re.sub(r"[^a-z0-9 ]+", " ", text.lower())` + `" ".join(split())` pattern — so `that s it`, `we re done`, `never mind`, `scratch that`, `forget it`. Keep them separate from `_FILLER_UTTERANCES`: one means "noise, no signal", the other "a deliberate close". Precedence: whole-utterance concluder, then ends-with void, then `_is_hallucination`, then normal.

Note `"thank you"` and `"thanks"` are currently in `_FILLER_UTTERANCES`; as concluders they must now classify as `INTENT_CONCLUDE`, not `INTENT_NOISE`. The trade is recorded in the spec (§3): Whisper invents "Thank you." from noise, so a cough can close a turn, accepted as harmless.

- [ ] **Step 4: Implement `transcribe_classified` and re-point `transcribe`**

`transcribe_classified` does today's `transcribe` work up to `_confident_text`, then returns `(text, classify_text(text))`. `transcribe` delegates and returns `""` unless the intent is `INTENT_NORMAL`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_stt_turn_intent.py tests/test_stt_filter.py tests/test_stt_hallucination.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

`scripts/mic_check.py:51` calls `transcribe` and will now print `""` for a concluder. That is the honest result for a diagnostic; leave it, and note it in the commit body.

```bash
git add voice/stt.py tests/test_stt_turn_intent.py
git commit -m "stt: classify a transcript as concluder, void, noise or normal"
```

---

### Task 4: Carry the confirmation verdict out of the agent loop

**Files:**
- Modify: `jarvisd/core.py` (`Assistant.__init__:201`, `execute_guarded:404-428`, `process:430-439`)
- Test: `tests/test_core_awaiting_confirmation.py` (create)

**Interfaces:**
- Consumes: nothing
- Produces: `Assistant.awaiting_confirmation: bool`, readable by the server after `process()` returns

- [ ] **Step 1: Write the failing tests**

```python
def test_a_confirmation_required_turn_sets_the_flag():
    ai = assistant_with_guard()
    ai.execute_guarded("delete_event", {"id": "x"})       # destructive -> CONFIRM_REQUIRED
    assert ai.awaiting_confirmation is True

def test_a_new_turn_resets_the_flag():
    ai = assistant_with_guard()
    ai.awaiting_confirmation = True
    ai.process("what time is it")
    assert ai.awaiting_confirmation is False

def test_an_allowed_tool_leaves_the_flag_false():
    ai = assistant_with_guard()
    ai.execute_guarded("get_weather", {})
    assert ai.awaiting_confirmation is False

def test_an_autonomous_turn_never_sets_the_flag():
    """execute_guarded is shared with the HA trigger rules, which never call
    process(), so a flag set there would leak into the next voice reply."""
    ai = assistant_with_guard()
    ai.execute_guarded("delete_event", {"id": "x"}, autonomous=True)
    assert ai.awaiting_confirmation is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_core_awaiting_confirmation.py -v`
Expected: FAIL with `AttributeError: awaiting_confirmation`

- [ ] **Step 3: Implement the flag**

Initialise `self.awaiting_confirmation = False` in `__init__`. Set it `True` in the `Verdict.CONFIRM_REQUIRED` branch of `execute_guarded` **only when `not autonomous`**. Reset it to `False` in `process()` beside the existing `self.guard.begin_turn()` call at `core.py:439`.

Do **not** use `guard._pending_confirmations` as the signal: `clear_pending()` has no production callers, so an armed-but-never-confirmed key persists for the life of the daemon and the flag would latch true forever.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_core_awaiting_confirmation.py tests/test_core.py tests/test_guard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvisd/core.py tests/test_core_awaiting_confirmation.py
git commit -m "core: expose a per-turn awaiting_confirmation flag"
```

---

### Task 5: Server decides the turn outcome and ships it

**Files:**
- Modify: `jarvisd/server.py` (`_handle_audio:313-355`, `_send_reply:357-367`, `_handle_json:287` for the `text_in` path)
- Test: `tests/test_server_turn_field.py` (create)

**Interfaces:**
- Consumes: `classify_text`, `transcribe_classified`, the `INTENT_*` constants (Task 3); `Assistant.awaiting_confirmation` (Task 4)
- Produces: an additive `"turn"` key on the reply JSON with one of `"open"`, `"closed"`, `"voided_open"`, `"voided_closed"`. The `reason: "unverified"` and empty-transcription frames keep their current shape and carry no `"turn"` key.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_completed_action_closes_the_mic():
    reply = await handle("turn the lights off", agent_says="Done.")
    assert reply["turn"] == "closed"

async def test_a_question_holds_the_mic_open():
    reply = await handle("delete my meeting", agent_says="Which one, the 3pm or the 4pm?")
    assert reply["turn"] == "open"

async def test_a_confirmation_request_holds_the_mic_open():
    reply = await handle("delete the 3pm meeting", awaiting_confirmation=True,
                         agent_says="That will delete it permanently. Confirm?")
    assert reply["turn"] == "open"

async def test_a_question_still_holds_open_when_a_notice_is_appended():
    """_append_drained_notices and _append_habit_trailer both end in a period,
    so the flag must be computed on what _process returned."""
    reply = await handle("delete my meeting", agent_says="Which one?",
                         pending_notice="Also, while you were away: a timer finished.")
    assert reply["turn"] == "open"

async def test_a_concluder_closes_without_calling_the_agent():
    calls = []
    reply = await handle("that's it", on_process=calls.append)
    assert reply["turn"] == "closed"
    assert calls == []            # no round trip, no token spend

async def test_a_concluder_is_ignored_while_a_confirmation_is_outstanding():
    """'that's it' is a plausible answer to a destructive-action prompt;
    swallowing it would hit the spec's anti-goal."""
    calls = []
    reply = await handle("that's it", previous_turn="open", on_process=calls.append)
    assert reply["turn"] != "closed"
    assert calls == ["that's it"]  # it reached the agent as a real answer

async def test_never_mind_voids_and_keeps_the_mic_open():
    calls = []
    reply = await handle("remind me at five, never mind", on_process=calls.append)
    assert reply["turn"] == "voided_open"
    assert calls == []
    assert reply["text"] == ""

async def test_forget_it_voids_and_closes():
    reply = await handle("remind me at five, forget it")
    assert reply["turn"] == "voided_closed"

async def test_an_unverified_frame_is_unchanged():
    reply = await handle_unverified()
    assert reply["reason"] == "unverified"
    assert "turn" not in reply
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server_turn_field.py -v`
Expected: FAIL — `KeyError: 'turn'`

- [ ] **Step 3: Classify before calling the agent, in `_handle_audio`**

Replace the `self.stt.transcribe` call with `transcribe_classified`. On `INTENT_CONCLUDE`, `INTENT_VOID_CONTINUE` or `INTENT_VOID_CLOSE`, send a reply frame with empty `text`, the `heard` string, `audio_follows: false` and the matching `"turn"` value — **without** calling `_process`. This placement also puts the exit upstream of `_handle_local` (`core.py:468-479`), which would otherwise answer a concluder with canned politeness and hold the window open.

Keep per-connection state (alongside `self._pending_interrupt`) recording the `"turn"` value of the previous reply. When it was `"open"`, skip concluder matching for this one turn and treat the utterance as normal — a confirmation answer must reach the agent.

- [ ] **Step 4: Compute the flag before the appenders**

In `_handle_audio`, compute `expects_reply` from the string `_process` returned **before** `_append_drained_notices` (`server.py:353`) and `_append_habit_trailer` (`server.py:354`) run: true when `self.assistant.awaiting_confirmation` or the reply's stripped text ends with `"?"`. Pass the resulting `"turn"` value into `_send_reply` and include it in the frame built at `server.py:365`. Add it to the `text_in` reply at `server.py:287` for symmetry; the CLI ignores unknown keys.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_server_turn_field.py tests/test_clients_protocol.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add jarvisd/server.py tests/test_server_turn_field.py
git commit -m "server: decide and ship the turn outcome on every reply"
```

---

### Task 6: Client obeys the turn outcome

**Files:**
- Modify: `clients/voice_client.py` (the `run` loop at `556-700`; the `_stopped_beep` docstring at `498-501`)
- Test: `tests/test_voice_client_turn.py` (create)

**Interfaces:**
- Consumes: the `"turn"` field (Task 5)
- Produces: nothing for later tasks

- [ ] **Step 1: Write the failing tests**

```python
def test_closed_rearms_the_wake_word():
    state = run_one_exchange(reply={"turn": "closed", "text": "Done."})
    assert state.in_conversation is False
    assert state.rearmed is True

def test_open_keeps_the_follow_up_window():
    state = run_one_exchange(reply={"turn": "open", "text": "Which one?"})
    assert state.in_conversation is True

def test_a_missing_turn_field_behaves_exactly_as_today():
    """Additive field: an older server must not change client behaviour."""
    state = run_one_exchange(reply={"text": "Done."})
    assert state.in_conversation is True

def test_voided_open_on_the_FIRST_utterance_reopens_the_window():
    """in_conversation is still False here, so 'reopen' must set it
    explicitly the way the barge-in branch does, or this drops to idle."""
    state = run_one_exchange(reply={"turn": "voided_open", "text": ""},
                             in_conversation=False)
    assert state.in_conversation is True
    assert state.rearmed is False
    assert state.printed_didnt_catch is False
    assert state.misfires == 0

def test_voided_closed_rearms():
    state = run_one_exchange(reply={"turn": "voided_closed", "text": ""})
    assert state.in_conversation is False
    assert state.rearmed is True

def test_a_barge_in_over_a_closing_reply_is_not_lost():
    """The close must be applied after _play_and_watch_reply returns
    not-interrupted, or the pending_barge_in branch is bypassed."""
    state = run_one_exchange(reply={"turn": "closed", "text": "Done."},
                             interrupted=True)
    assert state.pending_barge_in is True
    assert state.rearmed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_voice_client_turn.py -v`
Expected: FAIL — `in_conversation` is `True` after a `"closed"` reply

- [ ] **Step 3: Honour the field in the `run` loop**

Read `reply.get("turn")` and act on it **after** `_play_and_watch_reply` returns not-interrupted (`voice_client.py:677-686`), so the `interrupted → pending_barge_in → continue` branch still wins. Default (field absent) keeps today's `advance(productive=True)` path.

- `"closed"` / `"voided_closed"`: set `in_conversation = False`, then `rearm()`.
- `"voided_open"`: set `in_conversation = True` explicitly, leave `misfires` untouched, leave `active` `True` so no beep fires, and print nothing resembling "(didn't catch that)". Reopen the window with `UTTERANCE_SPEECH_TIMEOUT_SEC` rather than `FOLLOWUP_WINDOW_SEC` — 6s is short for "I'm about to rephrase".
- A voided frame must not take the empty-`heard` path at `voice_client.py:666-671`.

- [ ] **Step 4: Rewrite the stopped-beep docstring**

`voice_client.py:498-501` documents the opposite of the new behaviour ("No cue means it heard you and is thinking"). The mallet now plays after essentially every completed command; say so.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_voice_client_turn.py tests/test_voice_client.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add clients/voice_client.py tests/test_voice_client_turn.py
git commit -m "voice client: close, hold or void a turn as the server says"
```

---

### Task 7: Tray — an open mic looks different from idle

**Files:**
- Modify: `clients/activity.py` (new state constant), `clients/tray_app.py` (`STATE_COLORS:164-174`, `STATE_LABEL:178-186`, `effective_state:221-239`), `clients/voice_client.py` (`_capture_followup`)
- Test: `tests/test_tray_app.py` (extend; create if absent)

**Interfaces:**
- Consumes: nothing
- Produces: `activity.YOUR_TURN = "your_turn"`

- [ ] **Step 1: Write the failing tests**

```python
def test_your_turn_is_not_collapsed_into_listening():
    """ARMED renders identically to idle on purpose; an open mic must not."""
    act = {"state": activity.YOUR_TURN, "ts": NOW}
    assert effective_state(LISTENING, act, NOW) == activity.YOUR_TURN

def test_your_turn_has_its_own_colour_and_label():
    assert STATE_COLORS[activity.YOUR_TURN] == (90, 215, 230)
    assert STATE_LABEL[activity.YOUR_TURN] == "JARVIS — your turn…"

def test_your_turn_ring_is_hollow():
    """HEARING's filled ring means 'recording you right now'; it would sit
    filled while you are silent."""
    assert is_filled(activity.YOUR_TURN) is False

def test_followup_capture_publishes_your_turn():
    client._capture_followup()
    assert last_activity_write() == activity.YOUR_TURN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tray_app.py -k your_turn -v`
Expected: FAIL with `AttributeError: YOUR_TURN`

- [ ] **Step 3: Add the state and render it**

Define `YOUR_TURN = "your_turn"` in `clients/activity.py` beside the other states, with a comment distinguishing it from both `ARMED` (renders as idle) and `HEARING` (filled ring, actively recording). Add its `STATE_COLORS` and `STATE_LABEL` entries; leave it out of `_FILLED_STATES` and `_ANIMATED_STATES`; leave the `ARMED → LISTENING` collapse at `tray_app.py:232` as it is. Have `_capture_followup` write it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tray_app.py tests/test_voice_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

Note in the commit body that this state becomes rare once Task 5 lands — the open mic mostly stops existing, so Goal 4 is largely achieved by Goal 2 removing it. The rendering earns its place for confirmation turns, questions, and "never mind".

```bash
git add clients/activity.py clients/tray_app.py clients/voice_client.py tests/
git commit -m "tray: an open mic is visually distinct from idle"
```

---

### Task 8: Edge TTS — shorter budget, one retry, then fall back

**Files:**
- Modify: `voice/tts_edge.py` (`EdgeTTS.synthesize:54-72`, the shared-constant import comment at `27-31`)
- Test: `tests/test_tts_edge_retry.py` (create)

**Interfaces:**
- Consumes: nothing
- Produces: `EDGE_ATTEMPT_TIMEOUT_SEC = 5.0` and `EDGE_ATTEMPTS = 2` in `voice/tts_edge.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_failed_first_attempt_is_retried_once():
    edge = EdgeTTS()
    calls = fail_then_succeed(edge, failures=1)
    assert edge.synthesize("hello") is not None
    assert calls.count == 2

def test_two_failures_return_none_so_the_caller_falls_back():
    edge = EdgeTTS()
    calls = always_fail(edge)
    assert edge.synthesize("hello") is None
    assert calls.count == EDGE_ATTEMPTS

def test_a_success_is_not_retried():
    """Measured: 0.28-0.39s warm. The retry must add nothing to the ~95% path."""
    edge = EdgeTTS()
    calls = always_succeed(edge)
    assert edge.synthesize("hello") is not None
    assert calls.count == 1

def test_each_attempt_gets_a_fresh_result_dict():
    """A timed-out worker is abandoned, not cancelled, and still writes
    result['wav'] -- a shared dict would let attempt 1 hand its WAV back
    through attempt 2's slot."""
    edge = EdgeTTS()
    seen = attempt_result_dicts(edge, failures=1)
    assert seen[0] is not seen[1]

def test_the_per_attempt_budget_is_five_seconds():
    assert EDGE_ATTEMPT_TIMEOUT_SEC == 5.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tts_edge_retry.py -v`
Expected: FAIL — `calls.count == 1` after a failure

- [ ] **Step 3: Implement the retry inside `EdgeTTS.synthesize`**

Loop `EDGE_ATTEMPTS` times, each attempt spawning its worker with its **own** `result` dict and joining on `EDGE_ATTEMPT_TIMEOUT_SEC`. Return the first non-`None` WAV; return `None` after the last attempt so `_EdgeWithFallback` (`tts.py:103-109`) degrades to SAPI as it does today. Keep the retry here rather than in the wrapper, so the wrapper never needs to know Edge's attempt budget.

- [ ] **Step 4: Update the two stale comments**

The module comment at `tts_edge.py:27-31` says `SYNTH_TIMEOUT_SEC` is imported from `voice.tts` so "the daemon-freeze guard [doesn't] drift between the two TTS backends". A per-attempt budget breaks that sharing deliberately — rewrite it to say so, and note that a timed-out attempt abandons a live thread, so a doubly-failed reply now leaks two instead of one.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tts_edge_retry.py tests/test_tts*.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite and commit**

Run: `python -m pytest -q`
Expected: PASS

```bash
git add voice/tts_edge.py tests/test_tts_edge_retry.py
git commit -m "tts: 5s Edge attempts with one retry before the SAPI fallback"
```

---

## Verification

- [ ] `python -m pytest -q` — full suite green, including the slow tests
- [ ] Manual: say "hey jarvis", pause ~8s, then speak — the capture is still open
- [ ] Manual: "turn the lights off" — the mallet sounds immediately after "Done."
- [ ] Manual: "remind me at five, never mind" — nothing is set, nothing is spoken, the tray shows your turn
- [ ] Manual: "remind me at five, forget it" — nothing is set, the mallet sounds
- [ ] Manual: ask something that prompts a confirmation, answer "that's it" — it reaches the agent as an answer rather than closing the mic
- [ ] `grep -c "edge-tts synthesis timed out" logs/jarvisd.log` — no new 20s timeouts after the change
