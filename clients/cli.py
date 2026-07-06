"""Typed chat client for jarvisd.  Usage: python -m clients.cli"""

import asyncio
import json
import os
import sys

import websockets
import websockets.exceptions

from clients.protocol import ReceiveLoop


async def chat():
    url = os.getenv("JARVIS_URL", "ws://127.0.0.1:8765")
    try:
        async with websockets.connect(url) as ws:
            print("Connected to jarvisd. 'quit' to exit, 'reset' to clear history.\n")

            def on_proactive(text):
                print(f"\nJARVIS (proactive): {text}\n")

            # One background receiver owns the socket read so a proactive push
            # can print above the prompt at any time, even while we're blocked
            # waiting on typed input below.
            receiver = ReceiveLoop(ws, on_proactive_text=on_proactive)
            recv_task = asyncio.create_task(receiver.run())
            try:
                while True:
                    try:
                        user = (await asyncio.to_thread(input, "You: ")).strip()
                    except (KeyboardInterrupt, EOFError):
                        break
                    if not user:
                        continue
                    if user.lower() in ("quit", "exit", "q"):
                        break
                    msg = {"type": "reset"} if user.lower() == "reset" else {"type": "text_in", "text": user}
                    await ws.send(json.dumps(msg))
                    item = await receiver.get_reply(recv_task)
                    reply = item["data"]
                    # Any WAV that follows is discarded here -- the CLI never plays audio.
                    print(f"\nJARVIS: {reply.get('text') or reply.get('error')}\n")
            finally:
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                    pass
    except (ConnectionRefusedError, OSError):
        print(f"Cannot reach jarvisd at {url}. Start it with: python -m jarvisd")
        sys.exit(1)
    print("Bye.")


if __name__ == "__main__":
    asyncio.run(chat())
