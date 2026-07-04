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
        log.info("Audio in: %.1fs (%d bytes)", len(audio) / 16000, len(pcm))
        heard = await asyncio.to_thread(self.stt.transcribe, audio)
        if not heard.strip():
            log.info("Transcription empty — no reply generated")
            await ws.send(json.dumps({"type": "reply", "text": "", "heard": "", "audio_follows": False}))
            return
        log.info("Heard: %s", heard)
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
