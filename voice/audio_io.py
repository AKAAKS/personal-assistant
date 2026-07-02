"""Microphone capture and voice activity detection."""

import queue
import numpy as np
import sounddevice as sd
import webrtcvad

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_DURATION_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)


class MicrophoneStream:
    """Streams audio from the default microphone at 16kHz mono."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, frame_size: int = FRAME_SIZE):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stream = None

    def _callback(self, indata, frames, time_info, status):
        self._queue.put(indata[:, 0].copy())

    def start(self):
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=self.frame_size,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read_frame(self, timeout: float = 1.0) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self):
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class VoiceActivityDetector:
    """Detects speech boundaries using WebRTC VAD."""

    def __init__(self, aggressiveness: int = 2):
        self.vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.vad.is_speech(frame.tobytes(), SAMPLE_RATE)

    def record_until_silence(
        self,
        mic: MicrophoneStream,
        pre_buffer: list[np.ndarray] | None = None,
        max_seconds: float = 15.0,
        silence_ms: int = 800,
        speech_timeout_sec: float = 5.0,
    ) -> np.ndarray | None:
        """Record from mic until speech stops. Returns audio array or None."""
        frames = list(pre_buffer) if pre_buffer else []
        max_frames = int(max_seconds * 1000 / FRAME_DURATION_MS)
        silence_needed = int(silence_ms / FRAME_DURATION_MS)
        timeout_frames = int(speech_timeout_sec * 1000 / FRAME_DURATION_MS)

        speech_started = bool(pre_buffer)
        silent_count = 0
        no_speech_count = 0

        for _ in range(max_frames):
            frame = mic.read_frame(timeout=1.0)
            if frame is None:
                continue

            if self.is_speech(frame):
                speech_started = True
                silent_count = 0
                no_speech_count = 0
                frames.append(frame)
            elif speech_started:
                silent_count += 1
                frames.append(frame)
                if silent_count >= silence_needed:
                    break
            else:
                no_speech_count += 1
                if no_speech_count >= timeout_frames:
                    return None

        if not frames:
            return None
        return np.concatenate(frames)
