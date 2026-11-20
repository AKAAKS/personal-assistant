# JARVIS turn boundaries: timing, closing the mic, spoken exits

Date: 2026-11-20
Status: design, awaiting approval
Base: `master` @ `fc05523`, level with `origin/master`

## Intent and the why

JARVIS gets the boundaries of a spoken turn wrong in both directions. It is
too impatient at the start of a turn and too slow to let go at the end.

At the start, `_record_utterance` gives you 5 seconds to begin speaking
(`speech_timeout_sec=5.0`) and ends the capture 800ms after any pause
(`silence_ms=800`). Both are too tight for real speech. You say "hey jarvis",
then spend a moment working out how to phrase the thing, and it has already
given up. Or you start a sentence, pause to rephrase, and the capture closes
on the fragment.

At the end, every productive exchange sets `in_conversation = True`
unconditionally, which opens a 6-second follow-up window
(`FOLLOWUP_WINDOW_SEC`). The window only ever closes on a timeout or a
failure: silence, two misfires, repeated unverified captures, or an error.
There is no path for "that turn is finished" or "I am done talking to you".

The cost is concrete and daily:

- You ask for something while watching a video. The action completes, and
  then the mic sits open for 6 more seconds. You cannot go back to watching
  until it lets go, and there is no way to tell it to let go.
- You finish a command and turn back to dictating to something else. JARVIS
  is still listening, grabs that speech, and answers it. You say "I wasn't
  talking to you", it says "okay" — and then *still* holds the window open.
- You genuinely cannot tell by looking whether it is still listening. The
  tray renders an open mic and an idle wake-word wait identically, so the
  only way to know is to wait for the stopped-listening mallet.

Separately, roughly 5% of spoken turns come out in the robotic SAPI voice
instead of the neural Edge voice, which reads as a malfunction when it is in
fact a working fallback.

## Goals

1. More time to start a turn, and more tolerance for a mid-sentence pause.
2. The mic closes the moment a turn is finished, when nothing is expected
   back from you.
3. Spoken exits:
   - a **concluder** ("that's it", "thank you") closes the mic;
   - **"never mind"** voids the turn and keeps the mic open, because you are
     about to rephrase;
   - **"forget it"** voids the turn and closes the mic.
4. An open mic is visually distinct from idle in the tray.
5. The robotic voice shows up less often, and its cause is documented.

## Non-goals

- **Speaker-conditioned endpointing.** Capture currently endpoints on WebRTC
  VAD, which answers "is this speech?" and not "is this *his* speech?", so
  room audio from a video keeps resetting `silent_count` and the capture runs
  long and ships a contaminated clip. This is the real fix for talking to
  JARVIS over a playing video, and it is deferred to its own project by
  explicit decision.
- **Client-side keyword spotting**, and the two features that need it: a stop
  word to cut off a reply mid-sentence (today the only interrupt is saying
  "hey jarvis" over it), and a "wait" word to hold the mic open while you
  compose. Both must be recognised during playback or during capture, where
  no transcript exists yet. They belong with the endpointing work, since that
  is the same architectural move.
- **Trailing concluders on a real command** ("lights off, that's it").
  Dropped on purpose: Goal 2 already closes the mic after a completed action,
  so appending a concluder saves nothing, and an ends-with strip has a real
  trap — "remind me to say thank you" ends in "thank you".
- **Undoing an already-executed command.** See Constraints: a separate
  "never mind" utterance can only arrive after the command has run, so
  cancelling it would mean per-tool inverse operations.
- **Model-declared follow-up expectation.** The deterministic server-side
  rule below is the first version; having Claude set the flag itself is a
  clean upgrade later.
- **Voice enrollment quality**, including additional negative voiceprints for
  non-enrolled human voices. Investigated and set aside: enrollment is not
  the lever for any problem in this batch.

## Anti-goals

These would make the work a failure even if every item technically ships:

- The mic closing while you are still mid-sentence. Strictly worse than today.
- A voided turn being indistinguishable from "didn't hear you".
- **A real command being silently rejected as unverified.** This is the worst
  outcome available in this batch — JARVIS simply goes quiet, and it is hard
  to notice as a pattern. See Design §1.
- Losing a spoken "yes" or "no" confirmation. `MIN_SPEECH_MS` and
  `_FILLER_UTTERANCES` both carry comments explaining that a briskly spoken
  "Yes." is ~200-300ms and must survive, because destructive tool calls
  require spoken confirmation. Nothing here may weaken that.
- Adding latency to the Edge TTS path when it is working, which is ~95% of
  the time.
- Every turn feeling slower because trailing silence was raised too far.
- **A phrase list that silently never matches.** Failing closed here looks
  exactly like today's behaviour, so it could ship unnoticed. See Design §3.

## Constraints

- **Transcription is server-side.** The client sends PCM; `jarvisd/server.py`
  transcribes and builds the reply; the words come back in `reply["heard"]`
  *after* the reply exists. Nothing in this batch can react to words during
  capture or playback.
- **The mic is not capturing between send and reply.** The client blocks on
  `receiver.get_reply()` (`voice_client.py:639`) after `ws.send(pcm)`. So a
  *separate* "never mind" utterance can only arrive after the command has
  already run. Same-utterance trailing "never mind" is the only case where
  voiding is genuinely possible, and the only case this design implements.
- **`process()` returns a bare `str`** (`core.py:430`), and `server._process`
  returns exactly that (`server.py:244`). The server therefore **cannot** see
  the `CONFIRMATION REQUIRED` verdict, which is a tool-result *string* built
  in `execute_guarded` (`core.py:422`) and consumed inside `_claude_loop`.
  Any follow-up flag must be threaded out deliberately — see Approaches.
- **`guard._pending_confirmations` is not usable as that signal.**
  `clear_pending()` (`security/guard.py:119`) has no production callers, so an
  armed-but-never-confirmed key persists for the life of the daemon and the
  flag would latch true forever.
- **`transcribe()` discards the text it rejects.** `voice/stt.py:178` returns
  `""` when `_is_hallucination` fires, so the server never sees the raw string
  and cannot classify what was collapsed.
- **`_handle_local` is a second interception point.** `core.py:468-479`
  answers "thanks"/"thank you" and "ok/cool/got it/alright/sure" with canned
  replies before any model call, so a concluder reaching `process()` today
  already gets "You're welcome!" and still holds the window open.
- **Speaker verification runs before STT** (`server.py:331-343`) and emits
  `reason: "unverified"`, which the client must not treat as a misfire. The
  new states must not disturb that path. Note `accepts` **fails open** when
  `SPEAKER_VERIFY=0`.
- **Reply JSON fields must be additive.** `server.py` already establishes this
  with `reason`: "Additive field; older clients ignore it."

## Approaches

The substantive choice is **who decides that a turn needs no follow-up**.

1. **Client-side guess.** The client inspects `reply["text"]` and keeps the
   window open only if it ends in "?". No server change at all. Rejected: it
   buries turn policy in the client, and the client cannot see the
   confirmation state at all — the single most important case for holding the
   mic open.
2. **Model-declared.** Claude sets an `expects_reply` field on each turn. Most
   accurate, and it generalises to replies that invite a response without a
   question mark. Rejected for now: it is non-deterministic (the model can
   forget), it is untestable without a model, and it needs the same metadata
   threading as option 3 anyway. Kept as a future upgrade.
3. **Deterministic server-side rule, shipped as a flag.** *(chosen)* The
   server computes the flag from two things it can be *made* to see — the
   `CONFIRMATION REQUIRED` verdict and a trailing "?" on the reply — and adds
   it to the reply JSON. The client just obeys.

   **Correction to an earlier version of this document:** the reason for
   preferring this over option 2 is *not* that the server already knows the
   confirmation state. It does not (see Constraints). Both options require
   threading metadata out of the agent. The real distinction is that this
   rule is deterministic and unit-testable without a model, and it keeps turn
   policy on the server rather than in the model's hands.

Three smaller choices, all settled:

- **Trailing silence: flat raise vs. taper.** A taper would allow a long pause
  early in an utterance and tighten later. It gets both properties but adds
  real state to a carefully tuned loop. Chosen: **flat raise**, with taper
  held in reserve.
- **Signalling a voided turn: new beep vs. new tray state.** Chosen: **tray
  state**. A distinct open-mic rendering also fixes the standing "can't tell
  if it's still listening" problem everywhere, and adds no new sound.
- **Protecting the verification score from the longer silence.** Chosen:
  **trim trailing silence before verification** (user decision), over raising
  `min_duration_sec` to match, or shipping and watching the logs. It is
  strictly better than today's behaviour and decouples pause tolerance from
  verification permanently.

## The design

### 1. Timing

Make the two hardcoded values in `_record_utterance` configurable, as
everything else in `voice/audio_io.py` already is, and raise them:

- **Pre-speech timeout**, time to start talking: 5s → **~10s**. Nearly free;
  it only costs you when you wake JARVIS and then say nothing at all.
- **Trailing silence**: 800ms → **~1200ms**. This is paid on *every* turn as
  dead air before JARVIS starts thinking, which is why it goes to 1200 and
  not 1400.

**`_capture_followup` keeps 800ms.** Correcting an earlier version of this
document: both callers currently pass the *same literals* per call
(`voice_client.py:486` hardcodes `silence_ms=800` too), so "it keeps its own
values" was wrong — they must be deliberately separated. The justification
for separating them: once §2 lands, the only turns reaching
`_capture_followup` are confirmations and answers to direct questions, where
a tight 800ms endpoint is correct. Note
`tests/test_voice_client.py:84-91` pins the two callers to identical kwargs
under a test named `..._uses_the_same_vad_params_as_capture_utterance`; that
invariant is being deliberately broken and the test must be updated to
express the new one.

**Trim trailing silence before verification.** The longer pause tolerance
pads every clip by ~400ms, and `SpeakerVerifier.decide` keys off total clip
length (`speaker_verify.py:236,249`) against `min_duration_sec = 2.0`. The
module comment at `speaker_verify.py:65` states the current assumption
outright — "*captures close after 800ms of trailing silence, nearly every
real command is 1.0-1.8s*" — which is why the relaxed short-clip floor is the
common path rather than an edge case. Padding moves typical commands to
1.4-2.2s, straddling that boundary, while the added silence also dilutes the
pooled embedding downward. Measured weakest genuine clips are 0.290 at 1.6s
and 0.350 at 1.2s, so there is little headroom to spend.

So: strip trailing silence from the array handed to
`speaker_verifier.accepts`, and keep the **full** clip for `stt.transcribe`
(Whisper benefits from the trailing context, and its own confidence gates
handle silence). This improves today's scores as well as protecting the
change. `speaker_verify.py:65` and the comment block above
`min_duration_sec` must be updated, since they encode the 800ms assumption
as fact.

Also note `clients/enroll_voice.py:74` and `scripts/voice_calibrate.py:228`
hardcode 800ms and feed the voiceprint and the measured score bands. They are
left alone, but the trim must be applied consistently with how enrollment
builds the print, or the calibrated numbers describe clips of a different
shape than the ones being judged.

### 2. Close the mic when the turn is done

The server computes one piece of state per reply — whether it expects
something back — and ships it as an additive field. It is true when:

- the turn ended in a confirmation request (`CONFIRMATION REQUIRED`), or
- the spoken reply ends in a question mark.

Otherwise false. "Turn the lights off" → "Done." → false → the client closes
the mic and re-arms the wake word immediately.

**How the confirmation verdict reaches the server.** A per-turn flag on
`Assistant`, set where the verdict is produced in `execute_guarded`
(`core.py:421-423`), reset in `process()` next to `guard.begin_turn()`
(`core.py:439`), and read by `server._process` inside `self._lock` — the same
lock already guarding every other read of `assistant.conversation`.

The reset placement is load-bearing. `execute_guarded` is shared with the HA
trigger rules via `autonomous=True` (`core.py:412-414`, called from
`__main__.py`'s `run_direct`/`run_smart`), so without a per-turn reset a
machine-initiated turn could set the flag and have it leak into the next
voice reply.

**Compute the flag on the string returned from `_process`, before line 353.**
`_append_drained_notices` (`server.py:353`) and `_append_habit_trailer`
(`server.py:354`) both append sentences ending in a period, and
`strip_markdown` (`server.py:358`) can touch the tail as well. A reply that
genuinely ended in "?" would otherwise compute `false` whenever a notice or
habit trailer happened to be pending, closing the mic on a real question.

Chaining is **deliberately sacrificed**. After a completed action, "...and the
bedroom too" will require a fresh wake word. This was confirmed as acceptable.

### 3. Spoken exits

Three behaviours, distinguished by what happens to the turn and to the mic:

| Phrase | Turn runs? | Mic after |
|---|---|---|
| "that's it", "that's all", "thank you", "thanks", "we're done" | nothing to run | **closes** |
| "...remind me at five, **never mind**" | **discarded** | **stays open** |
| "...remind me at five, **forget it**" | **discarded** | **closes** |

Also in the void-and-stay-open group, because they always precede a
restatement: **"scratch that"**, **"disregard that"**.

Two matching rules, with deliberately different risk profiles:

- **Concluders** match only when the phrase is the **entire utterance**. This
  can be a broad list, because matching the whole utterance cannot eat part
  of a real command.
- **Void phrases** match at the **end** of an utterance. Anchoring to the end
  is the safety property: "never mind that, what's the weather" *starts* with
  the phrase and is untouched. The list stays narrow and distinctive. A
  voided turn is discarded before the LLM is called, which also saves a round
  trip and the token spend.

**Concluder matching is suppressed while a confirmation is outstanding**
(user decision). "that's it", "we're done" and similar are plausible spoken
answers to a destructive-action confirmation prompt, and swallowing one would
hit the anti-goal directly. The server remembers the previous reply's
follow-up flag per connection and skips concluder matching for that one turn.
This is why the concluder list is allowed to stay broad.

**"Thank you" stays in the list** (user decision), with the trade recorded:
`voice/stt.py:27-29` documents that "3s of noise comes back as 'Thank you.'",
so a cough can occasionally close a turn. Accepted as harmless — the mallet
announces it and a wake word restarts. This leans on speaker verification
running *before* STT, which rejects most such blips as `unverified` before the
concluder path sees them; that ordering is load-bearing, and it stops holding
if `SPEAKER_VERIFY=0`, where `accepts` fails open.

**Phrase lists must be stored pre-normalised.** `_is_hallucination` normalises
with `re.sub(r"[^a-z0-9 ]+", " ", text.lower())` (`stt.py:45`), which turns
"That's it." into `that s it` and "we're done" into `we re done`. A list
written with apostrophes matches **nothing**, silently — the anti-goal above.
Storing them pre-normalised also makes the "thats it" variant fall out for
free, since both forms normalise identically.

**Placement.** `transcribe()` discards rejected text, so the classification
cannot live in the server above it. Add an entry point in `stt.py` returning
text **plus** classification, keeping `transcribe()`'s existing contract for
its second caller (`scripts/mic_check.py:51`). The pure matching logic stays
unit-testable without a model or audio, as `_is_hallucination` and
`SpeakerVerifier.decide` already are. Only a subset of the concluder list is
affected by the `_is_hallucination` collapse — "that's it", "that's all" and
"we're done" are *not* currently collapsed, only "thank you"/"thanks"/"bye"
are — but the exit must also sit **upstream of `_handle_local`**
(`core.py:468-479`), which otherwise answers a concluder with canned
politeness and holds the window open.

**Client sequencing.** Three outcomes must be distinguishable on the wire:
*normal reply*, *voided and continue*, *voided and close*, alongside the
existing *unverified* and *empty transcription*. A voided turn must not take
the empty-`heard` path, which prints "(didn't catch that)" and burns a
misfire; it must print nothing of the kind and burn no misfire.

- The close must be applied **after** `_play_and_watch_reply` returns
  not-interrupted (`voice_client.py:677-686`). Acting on it earlier — right
  after `get_reply()` — would bypass the `interrupted → pending_barge_in →
  continue` branch and lose a barge-in over a non-question reply. The
  `pending_barge_in` branch then re-sets `in_conversation = True`
  (`voice_client.py:606-607`) for its own documented reason, so the
  `unverified` echo path is undisturbed.
- "Voided and continue" must **explicitly set `in_conversation = True`**, the
  way the `pending_barge_in` branch does. `in_conversation` otherwise only
  becomes true via `advance(productive=True)` (`voice_client.py:689`), so a
  "never mind" on the first post-wake-word utterance would find it `False`
  and "re-open the window" would be a no-op that drops straight to idle.
  `misfires` stays untouched and `active` stays `True` so no beep fires. The
  reopened window should use the **new longer pre-speech timeout** rather
  than `FOLLOWUP_WINDOW_SEC` — 6 seconds is short for "I am about to
  rephrase", and this is the one place that argument clearly applies.
- **Behavioural change to call out:** the stopped-listening mallet will now
  play after essentially every completed command. Its current contract is the
  opposite — "no cue means it heard you and is thinking"
  (`voice_client.py:498-501`). That comment needs rewriting.

### 4. Tray: an open mic looks different from idle

`effective_state` (`tray_app.py:221`, not `_render_state` as an earlier
version of this document said) collapses `ARMED` into `LISTENING` at line
232 — same cyan `(0, 180, 200)`, same hollow ring, same tooltip. And the
follow-up window claims no state at all: `HEARING` is written in exactly one
place, `voice_client.py:325`, inside `_record_utterance`, so
`_capture_followup` leaves the last-written `ARMED` on screen.

Add a third rendering, between idle and `HEARING`, for "the mic is open and
it is your turn". Do **not** reuse `HEARING`: that is the filled ring meaning
"recording you right now" (`_FILLED_STATES`, `tray_app.py:190`), and it would
sit filled while you are silent. `_capture_followup` writes the new state;
the existing collapse of `ARMED` into `LISTENING` stays as it is.

Worth stating plainly: once §2 lands this state becomes **rare**, because the
open mic mostly stops existing. Goal 4 is largely achieved by Goal 2
*removing* the open mic rather than by rendering it. The rendering earns its
place for the confirmation and question turns that remain, and for "never
mind".

### 5. Edge TTS: shorter budget, then retry, then fall back

The robotic voice is the pyttsx3/SAPI **fallback**, not an offline mode,
reached per-utterance whenever an Edge synthesis returns `None`. Measured
from `logs/jarvisd.log`: **9** timeouts at `SYNTH_TIMEOUT_SEC` (20s) and **7**
outright failures, the traced ones being `WSServerHandshakeError: 403` from
`speech.platform.bing.com` — Microsoft rejecting the request, unrelated to
connectivity. 16 fallbacks across ~333 spoken turns.

A single reply is always one `synthesize` call — there is no sentence
chunking anywhere — so one reply is always one voice. The apparent mid-reply
voice switch is two *separate* utterances: the reply (`server.py:361`) and a
proactive push such as a reminder (`server.py:403`). Independent network
calls, so a blip hits one and not the other.

**Measured live, 2026-11-20** (6 consecutive syntheses of a typical reply at
`en-GB-SoniaNeural`): **0.28-0.39s warm, 2.86s on the first (cold) call,
mean 0.75s, zero failures.** So the 20s timeout is roughly 60× the typical
case; anything approaching it is a dead connection, not slow synthesis.

Change: cut the first Edge attempt to **~5s** (ample for the 2.86s cold
start, ~13× the warm case), **retry once**, then fall back to SAPI.

**Honest accounting of the retry**, correcting an earlier version of this
document: the probe could not reproduce a 403, so "a retry will usually
succeed" is **unverified**. Zero failures in 6 attempts is consistent with
the ~5% log rate and simply does not exercise the path. What the measurement
*does* justify is that a retry costs ~0.3s in the typical case, so it is
nearly free whether or not it helps — and the real win is removing the 20s
stall, not the retry. Supporting but not conclusive: the two logged 403s
carry *different* `Sec-MS-GEC` tokens, so edge-tts already regenerates the
signature per attempt, which makes recovery plausible.

Implementation notes:

- **Use a separate constant, not `SYNTH_TIMEOUT_SEC`.** `tts_edge.py:27-31`
  imports it from `voice.tts` specifically so "the daemon-freeze guard
  [doesn't] drift between the two TTS backends". A per-attempt Edge budget
  necessarily breaks that sharing, so it needs its own named constant and
  that comment needs updating rather than silently invalidating.
- **Put the retry inside `EdgeTTS.synthesize`**, so `_EdgeWithFallback`
  (`tts.py:103-109`) does not need to know Edge's attempt budget.
- **Fresh `result` dict per attempt.** A timed-out attempt is *not*
  cancelled: `worker.join(timeout)` returns and abandons a live thread that
  still writes `result["wav"]` (`tts_edge.py:63-71`). Sharing the dict would
  let a first thread finishing at 6s hand its WAV back through the second
  attempt's slot. Two abandoned threads per failed reply now accumulate
  instead of one — acceptable, but it should be noted in the code.
- **Worst case is 5+5+20 = 30s**, against today's 20+20 = 40s. The earlier
  claim of "about the same as today" was only true if the SAPI leg is
  ignored.
- Not changed: Edge is attempted first on every call, so a fallback never
  sticks — it self-heals, and that stays true.

## Left to the builder

- Env var names and placement, following the conventions already in
  `voice/audio_io.py` and `voice/tts.py`, and their documentation in
  `.env.example`.
- The wire representation of the three turn outcomes: one additive string
  field alongside the existing `reason` is the suggested shape, since
  `ReceiveLoop` already ignores unknown JSON keys (`clients/protocol.py:12`).
  Whether `text_in` (the CLI path, `server.py:283-287`) also carries it — add
  it for symmetry; the CLI ignores it.
- The silence-trim implementation: threshold and whether to reuse
  `VoiceActivityDetector` or a simpler energy test. It must not trim into the
  final consonant of a short word, given `MIN_SPEECH_MS` exists precisely
  because a spoken "Yes." is ~200-300ms.
- The new tray state's name, colour, glyph and tooltip, its entries in
  `STATE_COLORS`/`STATE_LABEL`, and its `effective_state` branch — consistent
  with the existing rule that colour carries meaning while animation carries
  urgency.
- Keeping the concluder list **separate** from `_FILLER_UTTERANCES`: one means
  "noise, no signal", the other "deliberate close", and merging them would
  re-create the ambiguity §3 exists to remove.
- Pre-speech 10s now sits close to `max_seconds=15.0`. Leave `max_seconds`
  alone.
- Updating the tests that pin the changed values — `tests/test_voice_client.py:84-91`
  and the 800ms reasoning in `tests/test_speaker_verify.py:223` — and adding
  coverage for each new piece, following the existing discipline of testing
  policy without models or audio.
