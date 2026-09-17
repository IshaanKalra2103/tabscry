"""Chat with Google AI Mode from the terminal, driven through a browser extension."""

import argparse
import asyncio
import sys

from .bridge import Bridge
from .chat import Turn
from .themes import load_settings


async def one_shot(text: str, new: bool):
    bridge = Bridge()
    server = asyncio.create_task(bridge.serve())
    try:
        await asyncio.wait_for(bridge.connected.wait(), timeout=40)
    except TimeoutError:
        sys.exit("extension didn't connect within 40s")
    turn = Turn(text)
    async for msg in bridge.ask(text, new=new, keep=True, route="tab" if load_settings().get("route") == "tab" else "window"):
        if msg["type"] == "error":
            sys.exit(msg["message"])
        if msg["type"] == "done":
            turn.markdown, turn.sources, turn.note = msg["markdown"], msg.get("sources", []), msg.get("note")
            print(turn.answer_markdown())
    server.cancel()


def main():
    p = argparse.ArgumentParser(description="Google AI Mode in your terminal, via your browser")
    p.add_argument("query", nargs="*", help="ask once and print markdown (no TUI)")
    p.add_argument("--continue", dest="cont", action="store_true", help="one-shot: follow up in the existing tab")
    args = p.parse_args()
    if args.query:
        asyncio.run(one_shot(" ".join(args.query), new=not args.cont))
    else:
        from .app import Tabscry  # imported lazily: textual-image probes the terminal on import

        Tabscry().run()
