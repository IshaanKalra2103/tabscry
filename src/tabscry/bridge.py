"""Websocket server that the browser extension connects to."""

import asyncio
import itertools
import json
import os

import websockets

HOST, PORT = "127.0.0.1", int(os.environ.get("TABSCRY_PORT", 8765))  # extension is hardwired to 8765


class Bridge:
    """One extension connection at a time; each question gets its own message queue."""

    def __init__(self, on_status=None):
        self.conn = None
        self.pending: dict[str, asyncio.Queue] = {}
        self.ids = itertools.count(1)
        self.on_status = on_status or (lambda connected: None)
        self.connected = asyncio.Event()

    async def serve(self):
        async with websockets.serve(self._handler, HOST, PORT, max_size=32 * 1024 * 1024):
            await asyncio.Future()

    async def _handler(self, conn):
        self.conn = conn
        self.connected.set()
        self.on_status(True)
        keepalive = asyncio.create_task(self._ping(conn))
        try:
            async for raw in conn:
                msg = json.loads(raw)
                if (q := self.pending.get(msg.get("id"))) is not None:
                    await q.put(msg)
        except websockets.ConnectionClosed:
            pass  # browser closed / extension reloaded
        finally:
            keepalive.cancel()
            if self.conn is conn:
                self.conn = None
                self.connected.clear()
                self.on_status(False)
                for q in self.pending.values():
                    await q.put({"type": "error", "message": "extension disconnected"})

    async def _ping(self, conn):
        # app-level traffic keeps the MV3 service worker from being suspended
        while True:
            await asyncio.sleep(20)
            await conn.send(json.dumps({"type": "ping"}))

    async def ask(self, text: str, new: bool = False, keep: bool = False):
        """Yield chunk/image/done/error messages for one question."""
        if self.conn is None:
            yield {"type": "error", "message": "extension not connected (is the browser open with the extension loaded?)"}
            return
        rid = str(next(self.ids))
        q = self.pending[rid] = asyncio.Queue()
        try:
            await self.conn.send(json.dumps({"type": "ask", "id": rid, "text": text, "new": new, "keep": keep}))
            while True:
                msg = await q.get()
                yield msg
                if msg["type"] in ("done", "error"):
                    return
        finally:
            del self.pending[rid]

    async def focus(self) -> str | None:
        """Ask the extension to show the AI Mode page in a real tab. Returns an error message or None."""
        if self.conn is None:
            return "extension not connected"
        rid = str(next(self.ids))
        q = self.pending[rid] = asyncio.Queue()
        try:
            await self.conn.send(json.dumps({"type": "focus", "id": rid}))
            msg = await asyncio.wait_for(q.get(), timeout=10)
            return msg.get("message") if msg["type"] == "error" else None
        except TimeoutError:
            return "browser didn't respond"
        finally:
            del self.pending[rid]
