"""Typed chat client for jarvisd.  Usage: python -m clients.cli"""

import asyncio
import json
import os
import sys

import websockets


async def chat():
    url = os.getenv("JARVIS_URL", "ws://127.0.0.1:8765")
    try:
        async with websockets.connect(url) as ws:
            print("Connected to jarvisd. 'quit' to exit, 'reset' to clear history.\n")
            while True:
                try:
                    user = input("You: ").strip()
                except (KeyboardInterrupt, EOFError):
                    break
                if not user:
                    continue
                if user.lower() in ("quit", "exit", "q"):
                    break
                msg = {"type": "reset"} if user.lower() == "reset" else {"type": "text_in", "text": user}
                await ws.send(json.dumps(msg))
                reply = json.loads(await ws.recv())
                if reply.get("audio_follows"):
                    await ws.recv()  # CLI discards audio
                print(f"\nJARVIS: {reply.get('text') or reply.get('error')}\n")
    except (ConnectionRefusedError, OSError):
        print(f"Cannot reach jarvisd at {url}. Start it with: python -m jarvisd")
        sys.exit(1)
    print("Bye.")


if __name__ == "__main__":
    asyncio.run(chat())
