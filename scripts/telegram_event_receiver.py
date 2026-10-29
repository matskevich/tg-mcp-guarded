#!/usr/bin/env python3
"""Read-only MTProto push receiver. Requires a separately authenticated session.

No model, scheduler, Telegram write, or arbitrary command is invoked by this process.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sqlite3
import sys
from pathlib import Path


def session_key(path):
    # Compare in memory only. Never log or expose the authorization material over IPC.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT auth_key FROM sessions LIMIT 1").fetchone()
    if not row or not row[0]:
        raise ValueError("session has no authenticated key")
    return row[0]


def configure(args):
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=os.environ.get("TG_ENV_FILE") or None)
    session = args.session.expanduser().resolve()
    if not session.is_file():
        raise ValueError(
            "dedicated session file does not exist; authenticate it separately"
        )
    if not args.independent_session:
        raise ValueError(
            "confirm separately authenticated session with --independent-session"
        )
    sources = []
    for key in ("TG_READ_SESSION_PATH", "TG_ACTIONS_SESSION_PATH", "TG_SESSION_PATH"):
        raw = os.environ.get(key)
        if raw:
            source = Path(raw).expanduser().resolve()
            if source == session:
                raise ValueError(
                    "receiver must not reuse a configured read/actions session"
                )
            if source.is_file():
                sources.append(source)
    event_key = session_key(session)
    if any(session_key(source) == event_key for source in sources):
        raise ValueError(
            "receiver session copies a configured authorization; authenticate separately"
        )
    # Force guards before importing the client; inherited action settings cannot override these.
    os.environ.update(
        TG_SESSION_PATH=str(session),
        TG_RECEIVE_UPDATES="1",
        TG_SESSION_RUNTIME_MODE="direct",
        TG_SESSION_LOCK_MODE="exclusive",
        TG_BLOCK_DIRECT_TELETHON_WRITE="1",
        TG_ALLOW_DIRECT_TELETHON_WRITE="0",
        TG_ENFORCE_ACTION_PROCESS="1",
        TG_ACTION_PROCESS="0",
        TG_WRITE_CONTEXT="read_events",
        TG_AUTH_BOOTSTRAP="0",
        TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS="actions_mcp",
    )
    return session


def event_payload(message, chat_id, kind="new_message"):
    return {
        "kind": kind,
        "chat_id": chat_id,
        "message_id": message.id,
        "sender_id": message.sender_id,
        "outgoing": bool(message.out),
        "date": message.date.isoformat() if message.date else None,
        "edited_at": message.edit_date.isoformat() if message.edit_date else None,
        "text": (message.message or "")[:4096],
        "reply_to_message_id": getattr(message, "reply_to_msg_id", None),
        "has_media": message.media is not None,
        "media_type": type(message.media).__name__ if message.media else None,
    }


async def reconcile(client, spool, peers, safe_call):
    for peer in sorted(peers):
        cursor = spool.checkpoint(peer)
        if cursor is None:
            latest = await safe_call(
                client.get_messages, peer, limit=1, operation_type="api"
            )
            spool.seed(peer, latest[0].id if latest else 0)
            continue
        # Collect the entire bounded gap before committing its high-water mark.
        found = []
        for _ in range(11):
            rows = await safe_call(
                client.get_messages,
                peer,
                min_id=cursor,
                reverse=True,
                limit=100,
                operation_type="api",
            )
            found.extend(rows)
            if len(found) > 1000:
                raise RuntimeError("startup_gap_over_1000_messages; reconcile manually")
            if not rows or len(rows) < 100:
                break
            cursor = rows[-1].id
        for message in sorted(found, key=lambda item: item.id):
            if not message.out:
                spool.append(event_payload(message, peer))
        if found:
            spool.advance_checkpoint(peer, max(message.id for message in found))


def transport_connected(client):
    # is_connected() remains True during Telethon auto-reconnect. Use its
    # transport probe, failing closed if a dependency update removes it.
    probe = getattr(getattr(client, "_sender", None), "_transport_connected", None)
    return bool(probe and probe())


async def watch_transport(client, socket, interval=1):
    # Local state only: no Telegram requests, history polling or model turns.
    while True:
        if not transport_connected(client):
            async with socket.changed:
                socket.ready = False
                socket.changed.notify_all()
            raise RuntimeError("transport_lost_restart_for_reconciliation")
        await asyncio.sleep(interval)


async def run(args):
    session = configure(args)
    from telethon import events

    from tganalytics.infra.event_socket import EventSocket
    from tganalytics.infra.event_spool import EventSpool
    from tganalytics.infra.limiter import safe_call
    from tganalytics.infra.tele_client import get_client_for_session

    peers = set(args.peer)
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    with (args.state_dir / "receiver.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        spool = EventSpool(args.state_dir / "events.sqlite3", args.account_id, peers)
        client = get_client_for_session(str(session))
        socket = EventSocket(spool, lambda: transport_connected(client))
        queue = asyncio.Queue(maxsize=5000)
        fatal = asyncio.Event()

        async def receive(event):
            if event.chat_id in peers and not event.message.out:
                kind = (
                    "edited_message"
                    if isinstance(event, events.MessageEdited.Event)
                    else "new_message"
                )
                try:
                    queue.put_nowait(event_payload(event.message, event.chat_id, kind))
                except asyncio.QueueFull:
                    fatal.set()

        client.add_event_handler(receive, events.NewMessage(incoming=True))
        client.add_event_handler(receive, events.MessageEdited(incoming=True))
        server = None
        try:
            await client.connect()
            if not await safe_call(client.is_user_authorized, operation_type="api"):
                raise RuntimeError(
                    "event session is not authorized; no interactive login performed"
                )
            me = await safe_call(client.get_me, operation_type="api")
            if me.id != args.account_id:
                raise RuntimeError("event session account mismatch")
            # A new authorization has no cached access hashes. Populate a bounded
            # recent-dialog cache; only explicitly selected peers enter the spool.
            await safe_call(client.get_dialogs, limit=200, operation_type="api")
            await reconcile(client, spool, peers, safe_call)
            await safe_call(client.catch_up, operation_type="api")
            socket.ready = True
            server = await socket.start(args.state_dir / "events.sock")

            async def consume():
                while True:
                    await socket.append(await queue.get())

            worker = asyncio.create_task(consume())
            transport = asyncio.create_task(watch_transport(client, socket))
            failed = asyncio.create_task(fatal.wait())
            disconnected = asyncio.ensure_future(asyncio.shield(client.disconnected))
            try:
                done, _ = await asyncio.wait(
                    [worker, failed, disconnected, transport],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if worker in done:
                    await worker
                if transport in done:
                    await transport
                raise RuntimeError(
                    "receiver disconnected or overflowed; restart for reconciliation"
                )
            finally:
                for task in (worker, failed, disconnected, transport):
                    task.cancel()
                await asyncio.gather(
                    worker, failed, disconnected, transport, return_exceptions=True
                )
        finally:
            socket.ready = False
            if server:
                server.close()
                await server.wait_closed()
            await client.disconnect()
            spool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--independent-session", action="store_true")
    parser.add_argument("--account-id", required=True, type=int)
    parser.add_argument("--peer", required=True, action="append", type=int)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Underlying exceptions may contain paths/message bodies: log only the class.
        print(
            json.dumps({"status": "stopped", "error_type": type(exc).__name__}),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
