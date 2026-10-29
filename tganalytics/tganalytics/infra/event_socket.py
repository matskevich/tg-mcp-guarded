"""Local bounded event wait: wakes on receiver notification, never polls Telegram."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path


async def read_event_socket(
    path: str, after_seq: int = 0, limit: int = 20, timeout_seconds: int = 0
) -> dict:
    if not 0 <= timeout_seconds <= 50 or after_seq < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid event request")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=4194304), 2
        )
        try:
            writer.write(
                json.dumps(
                    {
                        "after_seq": after_seq,
                        "limit": limit,
                        "timeout_seconds": timeout_seconds,
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.readline(), timeout_seconds + 3)
            if not data:
                raise ValueError("receiver disconnected")
            return json.loads(data)
        finally:
            writer.close()
            await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        return {"error": "event_receiver_unavailable"}


class EventSocket:
    def __init__(self, spool):
        self.spool = spool
        self.changed = asyncio.Condition()
        self.ready = False
        self.waiters = 0

    async def append(self, payload):
        async with self.changed:
            inserted = self.spool.append(payload)
            if inserted:
                self.changed.notify_all()
        return inserted

    async def handle(self, reader, writer):
        try:
            if self.waiters >= 32:
                raise ValueError("too_many_waiters")
            self.waiters += 1
            try:
                line = await asyncio.wait_for(reader.readline(), 3)
                args = json.loads(line)
                if not isinstance(args, dict):
                    raise ValueError("invalid_request")
                after, limit, timeout = (
                    args.get("after_seq", 0),
                    args.get("limit", 20),
                    args.get("timeout_seconds", 0),
                )
                if (
                    any(type(x) is not int for x in (after, limit, timeout))
                    or after < 0
                    or not 1 <= limit <= 100
                    or not 0 <= timeout <= 50
                ):
                    raise ValueError("invalid_request")
                async with self.changed:
                    result = self.spool.read(after, limit)
                    if (
                        not result.get("events")
                        and not result.get("error")
                        and timeout
                        and self.ready
                    ):
                        try:
                            await asyncio.wait_for(
                                self.changed.wait_for(
                                    lambda: bool(
                                        self.spool.read(after, limit).get("events")
                                    )
                                    or not self.ready
                                ),
                                timeout,
                            )
                        except asyncio.TimeoutError:
                            pass
                        result = self.spool.read(after, limit)
                    result["receiver_ready"] = self.ready
            finally:
                self.waiters -= 1
        except (ValueError, TypeError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            result = {"error": "invalid_event_request"}
        try:
            writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self, path: Path):
        # Exclusive receiver lock must already be held by caller.
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise ValueError("event socket path exists and is not a socket")
            path.unlink()
        server = await asyncio.start_unix_server(self.handle, str(path), limit=4096)
        os.chmod(path, 0o600)
        return server
