"""Enroll your voice so JARVIS responds only to you.

Records a few spoken phrases, averages them into a voiceprint, and -- crucially
-- also records JARVIS's OWN voice coming back off your speaker and stores that
as a negative print. Verification then asks "is this closer to you or to
JARVIS?" instead of leaning on one absolute threshold, which is what made the
same sentence pass at 0.751 and fail at 0.748.

The derived threshold and margin are WRITTEN to data/verify_config.json. The
previous version only printed a suggestion for you to copy into .env by hand;
nobody did, so the stale 0.75 default stayed live and rejected 63% of real
utterances.

Run once, in a terminal, speaking from where you normally sit, with your usual
speaker switched on and at your usual volume:
    python -m clients.enroll_voice
"""

import datetime as dt
import json
import os
import time
import winsound

import numpy as np

from voice.audio_io import SAMPLE_RATE, MicrophoneStream, VoiceActivityDetector
from voice.speaker_verify import (
    CONFIG_PATH,
    DEFAULT_BACKEND,
    IMPOSTOR_PATH,
    VOICEPRINT_PATH,
    calibrate,
    cosine_similarity,
    embed_audio,
    load_encoder,
    make_voiceprint,
)
from voice.tts import make_tts

N_SAMPLES = 6
MIN_SAMPLES = 4

# A mix of lengths on purpose. Enrollment used to use only long sentences while
# runtime clips are bare commands, and embedding quality tracks clip length --
# which is why short commands scored 0.37-0.62 and 3s sentences scored
# 0.70-0.87 from the same voice. Covering both lengths makes the centroid
# represent what JARVIS actually hears.
PROMPTS = [
    "Hey Jarvis, what's the weather like today?",
    "Lights off.",
    "What's on my calendar tomorrow morning?",
    "Play some music.",
    "Jarvis, remind me to call home this evening.",
    "Run Claude in a terminal.",
]

# Spoken by JARVIS to build the negative print. These are shaped like real
# replies so the print matches what actually bleeds back into the mic.
JARVIS_LINES = [
    "The temperature at home is twenty one degrees.",
    "I've set a reminder for four this afternoon.",
    "The living room lights are now on.",
    "You have two events on your calendar tomorrow.",
    "I'm opening a terminal and running Claude now.",
]

# Enough audio for a usable embedding (0.5s); below this we re-prompt.
MIN_SAMPLES_LEN = 8000


def _record(mic, vad):
    return vad.record_until_silence(
        mic, max_seconds=15.0, silence_ms=800, speech_timeout_sec=12.0
    )


def _record_fixed(mic, seconds: float) -> np.ndarray:
    """Capture exactly `seconds` of audio, whatever is in the room."""
    frames = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = mic.read_frame(timeout=0.5)
        if frame is not None:
            frames.append(frame)
    return np.concatenate(frames) if frames else np.array([], dtype=np.int16)


def _wav_pcm(wav: bytes):
    """Decode WAV bytes to (int16 mono ndarray, sample_rate)."""
    import io
    import wave

    with wave.open(io.BytesIO(wav), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0]
    return pcm, rate


def _resample_to_16k(pcm: np.ndarray, src_rate: int) -> np.ndarray:
    """Linear resample. Only used for the fallback path, where the TTS is
    embedded directly rather than recorded off the speaker."""
    if src_rate == SAMPLE_RATE or len(pcm) == 0:
        return pcm
    n = int(len(pcm) * SAMPLE_RATE / src_rate)
    old = np.linspace(0.0, 1.0, len(pcm), endpoint=False)
    new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(new, old, pcm.astype(np.float64)).astype(np.int16)


def _speak_and_record(mic, wav: bytes) -> np.ndarray | None:
    """Play a JARVIS clip and record what the mic hears of it.

    Recording the echo -- rather than embedding the clean TTS -- is the point:
    the negative print then carries the Bluetooth codec, the speaker's
    colouration and the room, which is exactly what has to be rejected.
    """
    pcm, rate = _wav_pcm(wav)
    duration = len(pcm) / float(rate) if rate else 0.0
    if duration <= 0:
        return None
    import tempfile

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="jarvis_enroll_")
        os.close(fd)
        with open(tmp, "wb") as f:
            f.write(wav)
        mic.clear()
        winsound.PlaySound(tmp, winsound.SND_FILENAME | winsound.SND_ASYNC)
        # A little past the clip, to catch the Bluetooth-delayed tail too.
        heard = _record_fixed(mic, duration + 0.6)
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        return heard
    except Exception:
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _collect_voice(encoder, backend, mic, vad):
    """Prompt for spoken samples until we have enough. Returns embeddings."""
    embeddings = []
    i = 0
    while len(embeddings) < N_SAMPLES:
        prompt = PROMPTS[i % len(PROMPTS)]
        i += 1
        input(f'[{len(embeddings) + 1}/{N_SAMPLES}] Press Enter, then say:  "{prompt}"')
        print("  Listening...")
        mic.clear()
        audio = _record(mic, vad)
        if audio is None or len(audio) < MIN_SAMPLES_LEN:
            print("  (didn't catch that — let's try that one again)\n")
            continue
        embeddings.append(embed_audio(encoder, audio, backend))
        print(f"  got it ({len(audio) / SAMPLE_RATE:.1f}s)\n")
    return embeddings


def _collect_jarvis(encoder, backend, mic, tts):
    """Play JARVIS's own voice and embed what the mic hears. Returns (embeddings,
    recorded) where `recorded` says whether the echo path was actually used."""
    embeddings, recorded = [], False
    for line in JARVIS_LINES:
        wav = tts.synthesize(line)
        if wav is None:
            print("  (TTS unavailable for that line — skipping)")
            continue
        heard = _speak_and_record(mic, wav)
        if heard is not None and len(heard) >= MIN_SAMPLES_LEN and np.abs(heard).max() > 300:
            embeddings.append(embed_audio(encoder, heard, backend))
            recorded = True
            print(f"  heard it back ({len(heard) / SAMPLE_RATE:.1f}s, "
                  f"peak {int(np.abs(heard).max())})")
        else:
            # Speaker off, muted, or headphones: fall back to the clean TTS so
            # there is still a negative print, just a less room-accurate one.
            pcm, rate = _wav_pcm(wav)
            pcm = _resample_to_16k(pcm, rate)
            if len(pcm) >= MIN_SAMPLES_LEN:
                embeddings.append(embed_audio(encoder, pcm, backend))
                print("  (couldn't hear it back — used the clean TTS instead)")
    return embeddings, recorded


def main():
    backend = os.getenv("SPEAKER_VERIFY_BACKEND", DEFAULT_BACKEND)

    print("\n=== JARVIS voice enrollment ===")
    print("Two halves: first you speak, then JARVIS speaks and listens to itself.")
    print("Sit where you normally sit, and leave your usual speaker ON at your")
    print("usual volume — the second half measures what bleeds back into the mic.\n")

    print(f"Loading the voice model ({backend})...")
    encoder = load_encoder(backend)
    mic = MicrophoneStream()
    vad = VoiceActivityDetector(aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")))
    mic.start()

    embeddings, jarvis_embs, recorded = [], [], False
    try:
        embeddings = _collect_voice(encoder, backend, mic, vad)

        if len(embeddings) >= MIN_SAMPLES:
            print("\n--- Now JARVIS speaks. Stay quiet for a few seconds. ---")
            tts = make_tts(rate=int(os.getenv("TTS_RATE", "180")))
            jarvis_embs, recorded = _collect_jarvis(encoder, backend, mic, tts)
    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        mic.stop()

    if len(embeddings) < MIN_SAMPLES:
        print(f"Only captured {len(embeddings)} sample(s); need at least {MIN_SAMPLES}. "
              "Nothing saved — run it again.")
        return

    voiceprint = make_voiceprint(embeddings)
    impostor = make_voiceprint(jarvis_embs) if jarvis_embs else None

    user_sims = [cosine_similarity(e, voiceprint) for e in embeddings]
    user_gaps = ([cosine_similarity(e, voiceprint) - cosine_similarity(e, impostor)
                  for e in embeddings] if impostor is not None else [])
    impostor_sims = ([cosine_similarity(e, voiceprint) for e in jarvis_embs]
                     if impostor is not None else [])

    cal = calibrate(user_sims=user_sims, user_gaps=user_gaps, impostor_sims=impostor_sims)

    os.makedirs(os.path.dirname(VOICEPRINT_PATH), exist_ok=True)
    np.save(VOICEPRINT_PATH, voiceprint)
    if impostor is not None:
        np.save(IMPOSTOR_PATH, impostor)
    config = {
        "backend": backend,
        "threshold": cal["threshold"],
        "margin": cal["margin"],
        "n_samples": len(embeddings),
        "n_impostor_samples": len(jarvis_embs),
        "impostor_from_speaker": recorded,
        "user_sim_min": cal["user_sim_min"],
        "user_sim_mean": cal["user_sim_mean"],
        "impostor_sim_max": cal["impostor_sim_max"],
        "separated": cal["separated"],
        "enrolled_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"\nSaved voiceprint to {VOICEPRINT_PATH}  ({len(embeddings)} samples)")
    if impostor is not None:
        src = "recorded off your speaker" if recorded else "clean TTS (speaker not heard)"
        print(f"Saved JARVIS's negative print to {IMPOSTOR_PATH}  "
              f"({len(jarvis_embs)} samples, {src})")
    else:
        print("No negative print built — verification will be threshold-only, "
              "which is weaker against the TTS echo.")
    print(f"Saved calibration to {CONFIG_PATH}")

    print(f"\n  your voice:   min {cal['user_sim_min']:.3f}, mean {cal['user_sim_mean']:.3f}")
    if cal["impostor_sim_max"] is not None:
        print(f"  JARVIS:       max {cal['impostor_sim_max']:.3f}")
    print(f"  -> threshold  {cal['threshold']:.2f}   margin {cal['margin']:.2f}")

    if not cal["separated"]:
        print("\n  WARNING: your voice and JARVIS's scored too close together, so")
        print("  there was no clean gap to put the threshold in. Verification will")
        print("  be less reliable. Usually this means the samples were too short or")
        print("  the mic was too far away — try re-running closer to the mic.")

    print("\nRestart the daemon (tray: Quit + relaunch) for it to take effect.")


if __name__ == "__main__":
    main()
