"""Chat with Google AI Mode from the terminal, driven through a browser extension."""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys

from .bridge import ENGINE_PORT, PORT, Bridge, free_port
from .browser import Engine, install_chromium
from .chat import Turn
from .themes import load_settings, save_setting


async def one_shot(text: str, new: bool):
    settings = load_settings()
    engine = Engine(ENGINE_PORT or free_port())
    own = (settings.get("engine") or ("own" if engine.available else "browser")) == "own" and engine.available
    if own:
        engine.start()
    bridge = Bridge(port=engine.port if own else PORT)
    server = asyncio.create_task(bridge.serve())
    try:
        await asyncio.wait_for(bridge.connected.wait(), timeout=40)
    except TimeoutError:
        sys.exit("extension didn't connect within 40s")
    turn = Turn(text)
    async for msg in bridge.ask(text, new=new, keep=not own, route="tab" if load_settings().get("route") == "tab" else "window"):
        if msg["type"] == "error":
            sys.exit(msg["message"])
        if msg["type"] == "done":
            turn.markdown, turn.sources, turn.note = msg["markdown"], msg.get("sources", []), msg.get("note")
            print(turn.answer_markdown())
    server.cancel()
    engine.stop()  # the sandboxed browser lives only as long as the command


DEFAULT_FONT = "ABC Areal Mono"


def launch_with_font(font: str) -> int:
    """Open tabscry in a new Ghostty window using `font` (leaves your Ghostty config alone).

    A TUI can't change the terminal's font itself — the terminal owns that.
    """
    save_setting("font", font)
    binary = shutil.which("tabscry") or sys.argv[0]
    if sys.platform == "darwin" and os.path.exists("/Applications/Ghostty.app"):
        cmd = ["open", "-na", "Ghostty", "--args", f"--font-family={font}", "-e", binary]
    elif shutil.which("ghostty"):
        cmd = ["ghostty", f"--font-family={font}", "-e", binary]
    else:
        print(f"Ghostty not found. Set your terminal's font to {font!r} manually, e.g. in\n"
              f"~/.config/ghostty/config:  font-family = \"{font}\"")
        return 1
    print(f"opening tabscry in a new Ghostty window with {font!r}…")
    subprocess.Popen(cmd)
    return 0


def main():
    p = argparse.ArgumentParser(description="Google AI Mode in your terminal, via your browser")
    p.add_argument("query", nargs="*", help="ask once and print markdown (no TUI)")
    p.add_argument("--continue", dest="cont", action="store_true", help="one-shot: follow up in the existing tab")
    p.add_argument("--install-browser", action="store_true", help="download Chrome for Testing for tabscry's own browser")
    p.add_argument("--font", nargs="?", const="", metavar="NAME",
                   help=f"open tabscry in a new Ghostty window with this font (default: {DEFAULT_FONT!r}, remembered)")
    args = p.parse_args()
    if args.install_browser:
        install_chromium()
        return
    if args.font is not None:
        sys.exit(launch_with_font(args.font or load_settings().get("font") or DEFAULT_FONT))
    if args.query:
        asyncio.run(one_shot(" ".join(args.query), new=not args.cont))
    else:
        from .app import Tabscry  # imported lazily: textual-image probes the terminal on import

        Tabscry().run()
