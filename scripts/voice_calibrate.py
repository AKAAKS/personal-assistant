"""Measure the three numbers the voice pipeline was guessing at.

Run from the project root:  python scripts/voice_calibrate.py

1. OUTPUT LATENCY -- how long after PlaySound returns the sound actually
   reaches the room. winsound gives no completion callback, so the client
   infers the end of a reply from the WAV header; on Bluetooth that clock is
   150-500ms early, which both truncated replies mid-sentence and dropped the
   delayed tail into the follow-up window as a fake user turn.

2. YOUR SCORE BAND -- the similarity of several real utterances, so you can see
   where the accept threshold actually sits relative to your own voice.

3. JARVIS'S SCORE BAND -- the similarity of JARVIS's own voice recorded back
   off your speaker. The two bands must not overlap; the gap between them is
   where the threshold belongs.

Stage-by-stage reporting, like scripts/mic_check.py.
"""

import io
import os
import sys
import tempfile
import time
import wave
import winsound

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from voice.audio_io import SAMPLE_RATE, MicrophoneStream, VoiceActivityDetector
from voice.speaker_verify import (
    DEFAULT_BACKEND,
    cosine_similarity,
    embed_audio,
    load_config,
    load_encoder,
    load_impostor,
    load_voiceprint,
)

CHIRP_MS = 40
CHIRP_REPEATS = 5
LISTEN_SEC = 1.5
N_UTTERANCES = 5

JARVIS_LINES = [
    "The temperature at home is twenty one degrees.",
    "I've set a reminder for four this afternoon.",
    "The living room lights are now on.",
]


# --- 1. output latency ------------------------------------------------------

def _chirp() -> np.ndarray:
    """A short rising sweep: broadband enough to correlate sharply, short
    enough that the correlation peak is unambiguous."""
    n = int(SAMPLE_RATE * CHIRP_MS / 1000)
    t = np.arange(n) / SAMPLE_RATE
    f0, f1 = 800.0, 4000.0
    sweep = np.sin(2 * np.pi * (f0 * t + (f1 - f0) / (2 * t[-1]) * t**2))
    window = np.hanning(n)
    return (sweep * window * 20000).astype(np.int16)


def _as_wav(pcm: np.ndarray) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def _record_fixed(mic, seconds: float) -> np.ndarray:
    frames = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = mic.read_frame(timeout=0.5)
        if frame is not None:
            frames.append(frame)
    return np.concatenate(frames) if frames else np.array([], dtype=np.int16)


def _play_async(wav: bytes) -> str | None:
    """Write a WAV to a temp file and start async playback. Returns the path."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis_cal_")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(wav)
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    return path


def _unlink(path):
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def measure_latency(mic) -> float | None:
    """Median round-trip delay in seconds, or None if the chirp wasn't heard."""
    chirp = _chirp()
    wav = _as_wav(chirp)
    template = chirp.astype(np.float64)
    template -= template.mean()

    lags = []
    for i in range(CHIRP_REPEATS):
        mic.clear()
        path = None
        try:
            path = _play_async(wav)
            heard = _record_fixed(mic, LISTEN_SEC)
        finally:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            _unlink(path)

        if len(heard) < len(template) * 2:
            continue
        sig = heard.astype(np.float64)
        sig -= sig.mean()
        corr = np.correlate(sig, template, mode="valid")
        peak = int(np.argmax(np.abs(corr)))
        # Reject a flat correlation: that means we never actually heard it.
        if np.abs(corr[peak]) < 4 * np.median(np.abs(corr)) + 1e-9:
            continue
        lags.append(peak / SAMPLE_RATE)
        time.sleep(0.2)

    if not lags:
        return None
    return float(np.median(lags))


# --- 2 + 3. score bands ----------------------------------------------------

def _speak_and_record(mic, wav: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav), "rb") as w:
        duration = w.getnframes() / float(w.getframerate() or SAMPLE_RATE)
    mic.clear()
    path = None
    try:
        path = _play_async(wav)
        return _record_fixed(mic, duration + 0.6)
    finally:
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        _unlink(path)


def _band(label, sims):
    if not sims:
        print(f"  {label}: (no samples)")
        return None, None
    lo, hi = min(sims), max(sims)
    print(f"  {label}: min {lo:.3f}  max {hi:.3f}  mean {sum(sims)/len(sims):.3f}")
    return lo, hi


def main():
    backend = os.getenv("SPEAKER_VERIFY_BACKEND") or load_config().get("backend") or DEFAULT_BACKEND
    mic = MicrophoneStream()
    mic.start()

    try:
        print("=== 1. Output latency ===")
        print(f"Playing {CHIRP_REPEATS} short chirps through your speaker — stay quiet...")
        latency = measure_latency(mic)
        if latency is None:
            print("  Could not hear the chirp back. Is the speaker on and unmuted,")
            print("  and is the mic the default input device? (Headphones will")
            print("  never pick it up — that's fine, latency barely matters then.)")
        else:
            ms = int(round(latency * 1000))
            print(f"  Round-trip delay: {ms} ms")
            print(f"  -> put  OUTPUT_LATENCY_MS={ms}  in your .env")
            if ms > 400:
                print("     (that's high even for Bluetooth — a wired speaker would")
                print("      make barge-in noticeably more responsive)")

        print("\n=== 2. Your score band ===")
        voiceprint = load_voiceprint(backend=backend)
        if voiceprint is None:
            print("  No usable voiceprint for this backend. Enroll first:")
            print("    python -m clients.enroll_voice")
            return
        cfg = load_config()
        # Resolve per backend. Hardcoding 0.75/0.10 here meant that with no
        # verify_config.json this diagnostic judged ECAPA scores (band
        # 0.29-0.78) against a resemblyzer threshold and printed REJECT for
        # every genuine utterance -- the exact false alarm it exists to
        # prevent, from the tool you reach for when verification misbehaves.
        from voice.speaker_verify import (
            BACKEND_DEFAULT_MARGIN,
            BACKEND_DEFAULT_THRESHOLD,
        )
        threshold = float(os.getenv(
            "SPEAKER_VERIFY_THRESHOLD",
            cfg.get("threshold", BACKEND_DEFAULT_THRESHOLD.get(backend, 0.25))))
        margin = float(os.getenv(
            "SPEAKER_VERIFY_MARGIN",
            cfg.get("margin", BACKEND_DEFAULT_MARGIN.get(backend, 0.24))))
        impostor = load_impostor(backend=backend)

        print(f"Loading the voice model ({backend})...")
        encoder = load_encoder(backend)
        vad = VoiceActivityDetector(aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")))

        print(f"\nSay the SAME short sentence {N_UTTERANCES} times, the same way each")
        print("time. That is the exact thing that used to pass and fail at random.\n")
        user_sims = []
        for i in range(N_UTTERANCES):
            input(f"[{i + 1}/{N_UTTERANCES}] Press Enter, then speak: ")
            mic.clear()
            audio = vad.record_until_silence(
                mic, max_seconds=15.0, silence_ms=800, speech_timeout_sec=10.0
            )
            if audio is None or len(audio) < 8000:
                print("  (didn't catch that)")
                continue
            emb = embed_audio(encoder, audio, backend)
            sim = cosine_similarity(emb, voiceprint)
            user_sims.append(sim)
            gap = ("" if impostor is None
                   else f"  gap {sim - cosine_similarity(emb, impostor):+.3f}")
            verdict = "accept" if sim >= threshold else "REJECT"
            print(f"  {len(audio)/SAMPLE_RATE:.1f}s  sim {sim:.3f}{gap}  -> {verdict}")

        print("\n=== 3. JARVIS's score band (the echo) ===")
        jarvis_sims = []
        try:
            from voice.tts import make_tts_from_env
            tts = make_tts_from_env()
            print("JARVIS will speak — stay quiet...")
            for line in JARVIS_LINES:
                wav = tts.synthesize(line)
                if wav is None:
                    continue
                heard = _speak_and_record(mic, wav)
                if len(heard) < 8000 or np.abs(heard).max() <= 300:
                    print("  (didn't hear it back — speaker off, or headphones)")
                    continue
                emb = embed_audio(encoder, heard, backend)
                sim = cosine_similarity(emb, voiceprint)
                jarvis_sims.append(sim)
                flag = "  <-- WOULD BE ACCEPTED AS YOU" if sim >= threshold else ""
                print(f"  sim {sim:.3f}{flag}")
        except Exception as e:
            print(f"  TTS unavailable: {e}")

        print("\n=== Verdict ===")
        print(f"  threshold {threshold:.2f}, margin {margin:.2f}, "
              f"negative print {'loaded' if impostor is not None else 'MISSING'}")
        u_lo, _ = _band("you   ", user_sims)
        _, j_hi = _band("jarvis", jarvis_sims)
        if u_lo is not None and j_hi is not None:
            if j_hi < u_lo:
                print(f"  Clean gap of {u_lo - j_hi:.3f} between the bands. Good.")
                if not (j_hi < threshold <= u_lo):
                    print("  But the threshold is NOT inside that gap — re-run")
                    print("    python -m clients.enroll_voice")
                    print("  to derive it from these measurements.")
            else:
                print("  The bands OVERLAP. No single threshold can separate them;")
                print("  the margin rule against the negative print is doing the")
                print("  real work. If you have no negative print, re-enroll.")
    finally:
        mic.stop()


if __name__ == "__main__":
    main()
