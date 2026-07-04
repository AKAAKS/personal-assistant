"""Voice-path diagnostic: mic -> VAD -> Whisper, each stage reported.

Run from the project root:  python scripts/mic_check.py
Speak a sentence when prompted.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from voice.audio_io import MicrophoneStream, VoiceActivityDetector, SAMPLE_RATE

RECORD_SECONDS = 4


def main():
    mic = MicrophoneStream()
    vad = VoiceActivityDetector(aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")))

    mic.start()
    print(f"Recording {RECORD_SECONDS}s — speak a sentence now...")
    frames = []
    deadline = time.monotonic() + RECORD_SECONDS
    while time.monotonic() < deadline:
        frame = mic.read_frame(timeout=1.0)
        if frame is not None:
            frames.append(frame)
    mic.stop()

    if not frames:
        print("FAIL: no audio frames from the microphone. Check the default input device.")
        return

    audio = np.concatenate(frames)
    peak = int(np.abs(audio).max())
    speech_frames = sum(1 for f in frames if vad.is_speech(f))
    ratio = speech_frames / len(frames)

    print(f"\nMic:     {len(audio) / SAMPLE_RATE:.1f}s captured, peak amplitude {peak}/32767 "
          f"{'(OK)' if peak > 1000 else '(TOO QUIET — check mic level/device)'}")
    print(f"VAD:     {speech_frames}/{len(frames)} frames flagged as speech ({ratio:.0%}) "
          f"{'(OK)' if ratio > 0.1 else '(VAD hears no speech — try VAD_AGGRESSIVENESS=1)'}")

    print("\nLoading Whisper...")
    from voice.stt import SpeechToText
    stt = SpeechToText(
        model_size=os.getenv("WHISPER_MODEL", "small"),
        device=os.getenv("WHISPER_DEVICE", "cuda"),
        compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8_float16"),
    )
    text = stt.transcribe(audio)
    print(f"Whisper: {text!r} {'(OK)' if text else '(EMPTY — mic/VAD stages above should say why)'}")


if __name__ == "__main__":
    main()
