"""Websocket server that the browser extension connects to."""

import asyncio
import contextlib
import errno
import fcntl
import itertools
import json
import os
import socket
from pathlib import Path

import websockets
from websockets.protocol import State

HOST = "127.0.0.1"
PORT = int(os.environ.get("TABSCRY_PORT", 8765))  # extension in your own browser
ENGINE_PORT = int(os.environ.get("TABSCRY_ENGINE_PORT", 8766))  # tabscry's own Chromium


RECONNECT_GRACE = 25  # seconds to wait for a suspended service worker to come back


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


class PortBusy(Exception):
    """Another tabscry already owns this port."""


def reserve_port(port: int, home: Path) -> object:
    """Claim a port for this process with a lock file.

    Without it a second tabscry binds nothing, kills the first one's browser and steals its
    extension — so both sessions break. The lock is released when the process exits.
    """
    home.mkdir(parents=True, exist_ok=True)
    handle = open(home / f"port-{port}.lock", "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno not in (errno.EAGAIN, errno.EACCES):
            raise
        handle.seek(0)
        owner = handle.read().strip() or "another process"
        handle.close()
        raise PortBusy(f"tabscry is already running on port {port} (pid {owner})") from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


class Bridge:
    """One extension connection at a time; each question gets its own message queue."""

    def __init__(self, on_status=None, port: int = PORT, token: str | None = None):
        self.port = port
        self.token = token  # engine A: only tabscry's own browser knows it
        self.conn = None
        self.pending: dict[str, asyncio.Queue] = {}
        self.ids = itertools.count(1)
        self.on_status = on_status or (lambda connected: None)
        self.connected = asyncio.Event()

    async def serve(self):
        async with websockets.serve(self._handler, HOST, self.port, max_size=32 * 1024 * 1024):
            await asyncio.Future()

    async def _handler(self, conn):
        try:
            hello = json.loads(await asyncio.wait_for(conn.recv(), timeout=10))
        except (TimeoutError, ValueError, websockets.ConnectionClosed):
            return await conn.close(code=4002, reason="expected hello")
        if self.token and hello.get("token") != self.token:
            # a browser from another tabscry run (or a stale one) dialling our port
            return await conn.close(code=4001, reason="not this session's browser")
        if self.conn is not None and self.conn.state is State.OPEN:
            return await conn.close(code=4003, reason="another browser is already connected")

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

    async def ask(self, text: str, new: bool = False, keep: bool = False, route: str = "window"):
        """Yield chunk/image/done/error messages for one question."""
        if self.conn is None:
            # the service worker may just be asleep between questions; give it a moment to dial back
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.connected.wait(), timeout=RECONNECT_GRACE)
        if self.conn is None:
            yield {"type": "error", "message": "extension not connected (is the browser open with the extension loaded?)"}
            return
        rid = str(next(self.ids))
        q = self.pending[rid] = asyncio.Queue()
        try:
            await self.conn.send(json.dumps({"type": "ask", "id": rid, "text": text, "new": new, "keep": keep, "route": route}))
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
