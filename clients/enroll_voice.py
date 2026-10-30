"""Enroll your voice so JARVIS responds only to you.

Records a few spoken phrases from the same mic the voice client uses, averages
them into a voiceprint, and saves it to data/voiceprint.npy. After this, the
daemon (on its next restart) will silently ignore any voice that isn't yours —
including its own TTS bleeding back through a room speaker.

Run once, in a terminal, speaking from where you normally sit:
    python -m clients.enroll_voice
"""

import os

import numpy as np

from voice.audio_io import MicrophoneStream, VoiceActivityDetector
from voice.speaker_verify import (
    VOICEPRINT_PATH,
    cosine_similarity,
    embed_audio,
    load_encoder,
    make_voiceprint,
)

N_SAMPLES = 5
MIN_SAMPLES = 3
PROMPTS = [
    'Hey Jarvis, what\'s the weather like today?',
    'Jarvis, turn on the living room lights.',
    'What\'s on my calendar tomorrow morning?',
    'Set a timer for ten minutes and play some music.',
    'Jarvis, remind me to call home this evening.',
]


def _record(mic, vad):
    return vad.record_until_silence(
        mic, max_seconds=15.0, silence_ms=800, speech_timeout_sec=12.0
    )


def main():
    print("\n=== JARVIS voice enrollment ===")
    print("Speak each line naturally, in your normal voice and your usual seat.")
    print("You can say the exact words or your own — the words don't matter, your voice does.\n")

    print("Loading the voice model...")
    encoder = load_encoder()
    mic = MicrophoneStream()
    vad = VoiceActivityDetector(aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")))
    mic.start()

    embeddings = []
    try:
        i = 0
        while len(embeddings) < N_SAMPLES:
            prompt = PROMPTS[i % len(PROMPTS)]
            i += 1
            input(f'[{len(embeddings) + 1}/{N_SAMPLES}] Press Enter, then say:  "{prompt}"')
            print("  Listening...")
            mic.clear()
            audio = _record(mic, vad)
            if audio is None or len(audio) < 1600:
                print("  (didn't catch that — let's try that one again)\n")
                continue
            embeddings.append(embed_audio(encoder, audio))
            print(f"  got it ({len(audio) / 16000:.1f}s)\n")
    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        mic.stop()

    if len(embeddings) < MIN_SAMPLES:
        print(f"Only captured {len(embeddings)} sample(s); need at least {MIN_SAMPLES}. "
              "Nothing saved — run it again.")
        return

    voiceprint = make_voiceprint(embeddings)
    sims = [cosine_similarity(e, voiceprint) for e in embeddings]
    lo, avg = min(sims), sum(sims) / len(sims)
    # A new voice should score below your own samples' spread; leave a margin
    # under your weakest sample so you're never rejected, but not so low that
    # others slip through.
    suggested = round(max(0.60, lo - 0.05), 2)

    os.makedirs(os.path.dirname(VOICEPRINT_PATH), exist_ok=True)
    np.save(VOICEPRINT_PATH, voiceprint)

    print(f"\nSaved voiceprint to {VOICEPRINT_PATH}  ({len(embeddings)} samples)")
    print(f"Your samples' self-similarity: min {lo:.3f}, avg {avg:.3f}")
    print(f"Suggested SPEAKER_VERIFY_THRESHOLD={suggested}  (default is 0.75)")
    if suggested != 0.75:
        print(f"  -> add  SPEAKER_VERIFY_THRESHOLD={suggested}  to your .env if JARVIS "
              "ignores you (too high) or answers others (too low).")
    print("\nRestart the daemon (tray: Quit + relaunch) for it to take effect.")


if __name__ == "__main__":
    main()
