import asyncio
import datetime
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tganalytics.infra.event_socket import EventSocket, read_event_socket
from tganalytics.infra.event_spool import EventSpool

spec = importlib.util.spec_from_file_location(
    "receiver", Path(__file__).parents[1] / "scripts/telegram_event_receiver.py"
)
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


def payload(mid=1, peer=10, text="synthetic message"):
    return {"chat_id": peer, "message_id": mid, "text": text, "outgoing": False}


def test_durable_dedup_and_binding(tmp_path):
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path, 1, {10})
    assert spool.append(payload())
    assert not spool.append(payload())
    assert not spool.append(payload(peer=20))
    assert not spool.append(dict(payload(), outgoing=True))
    assert spool.checkpoint(10) == 1
    stream_id = spool.read(0)["stream_id"]
    spool.close()
    spool = EventSpool(path, 1, {10})
    assert len(spool.read(0)["events"]) == 1
    assert spool.read(0)["stream_id"] == stream_id
    assert not spool.read(1)["events"]
    spool.close()
    with pytest.raises(ValueError, match="binding"):
        EventSpool(path, 2, {10})


def test_retention_reports_gap_without_false_duplicate_gap(tmp_path):
    spool = EventSpool(tmp_path / "events.sqlite3", 1, {10}, max_events=2)
    spool.append(payload(1))
    first = spool.read(0)["next_cursor"]
    spool.append(payload(1))  # SQLite AUTOINCREMENT may leave a sequence hole.
    spool.append(payload(2))
    assert "error" not in spool.read(first)
    spool.append(payload(3))
    spool.append(payload(4))
    assert spool.read(first)["error"] == "cursor_expired"
    spool.close()


@pytest.mark.asyncio
async def test_wait_wakes_once_replays_cursor_and_times_out(tmp_path):
    spool = EventSpool(tmp_path / "events.sqlite3", 1, {10})
    service = EventSocket(spool)
    service.ready = True
    path = tmp_path / "events.sock"
    server = await service.start(path)
    try:
        task = asyncio.create_task(read_event_socket(str(path), timeout_seconds=2))
        await asyncio.sleep(0.05)
        assert not task.done()
        await service.append(payload())
        result = await task
        assert len(result["events"]) == 1
        assert (await read_event_socket(str(path)))["events"] == result["events"]
        assert not (await read_event_socket(str(path), result["next_cursor"]))["events"]
        assert not (
            await read_event_socket(str(path), result["next_cursor"], timeout_seconds=1)
        )["events"]
    finally:
        server.close()
        await server.wait_closed()
        spool.close()


@pytest.mark.asyncio
async def test_missing_receiver_and_invalid_request(tmp_path):
    assert (await read_event_socket(str(tmp_path / "missing")))[
        "error"
    ] == "event_receiver_unavailable"
    with pytest.raises(ValueError):
        await read_event_socket("unused", timeout_seconds=51)


def test_configure_refuses_shared_session_and_forces_guards(tmp_path, monkeypatch):
    session = tmp_path / "events.session"
    with sqlite3.connect(session) as db:
        db.execute("CREATE TABLE sessions(auth_key BLOB)")
        db.execute("INSERT INTO sessions VALUES (?)", (b"synthetic-event-key",))
    args = SimpleNamespace(session=session, independent_session=True)
    monkeypatch.setenv("TG_READ_SESSION_PATH", str(session))
    with pytest.raises(ValueError, match="reuse"):
        receiver.configure(args)
    copied = tmp_path / "copied.session"
    copied.write_bytes(session.read_bytes())
    monkeypatch.setenv("TG_READ_SESSION_PATH", str(copied))
    with pytest.raises(ValueError, match="copies"):
        receiver.configure(args)
    monkeypatch.delenv("TG_READ_SESSION_PATH")
    monkeypatch.delenv("TG_SESSION_PATH", raising=False)
    monkeypatch.delenv("TG_ACTIONS_SESSION_PATH", raising=False)
    monkeypatch.setenv("TG_ALLOW_DIRECT_TELETHON_WRITE", "1")
    import os

    with patch.dict(os.environ):
        receiver.configure(args)
        assert os.environ["TG_ALLOW_DIRECT_TELETHON_WRITE"] == "0"
        assert os.environ["TG_ACTION_PROCESS"] == "0"


@pytest.mark.asyncio
async def test_restart_reconciles_without_replaying_bootstrap(tmp_path):
    spool = EventSpool(tmp_path / "events.sqlite3", 1, {10})
    now = datetime.datetime.now(datetime.timezone.utc)
    message = SimpleNamespace(
        id=20,
        out=False,
        sender_id=2,
        date=now,
        edit_date=None,
        message="synthetic",
        media=None,
    )
    client = SimpleNamespace(get_messages=AsyncMock(return_value=[message]))

    async def safe(fn, *args, **kwargs):
        kwargs.pop("operation_type", None)
        return await fn(*args, **kwargs)

    await receiver.reconcile(client, spool, {10}, safe)
    assert spool.checkpoint(10) == 20
    assert not spool.read(0)["events"]
    message.id = 21
    await receiver.reconcile(client, spool, {10}, safe)
    assert spool.read(0)["events"][0]["message_id"] == 21
    spool.close()


def test_dedupe_outlives_payload_retention_and_restart(tmp_path):
    path = tmp_path / "events.sqlite3"
    spool = EventSpool(path, 1, {10}, max_events=2)
    for mid in range(1, 4):
        spool.append(payload(mid))
    spool.close()
    spool = EventSpool(path, 1, {10}, max_events=2)
    assert not spool.append(payload(1))
    result = spool.read(0)
    assert result["history_truncated"]
    assert result["retention_floor"] > 0
    for mid in range(4, 30):
        spool.append(payload(mid))
    assert spool.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 20
    assert spool.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    spool.close()


@pytest.mark.asyncio
async def test_reconcile_advances_over_outgoing_and_does_not_advance_on_gap(tmp_path):
    spool = EventSpool(tmp_path / "events.sqlite3", 1, {10})
    spool.seed(10, 1)
    client = SimpleNamespace(
        get_messages=AsyncMock(return_value=[SimpleNamespace(id=2, out=True)])
    )

    async def safe(fn, *args, **kwargs):
        kwargs.pop("operation_type", None)
        return await fn(*args, **kwargs)

    await receiver.reconcile(client, spool, {10}, safe)
    assert spool.checkpoint(10) == 2
    assert not spool.read(0)["events"]
    client.get_messages = AsyncMock(
        side_effect=[
            [SimpleNamespace(id=i, out=True) for i in range(start, start + 100)]
            for start in range(3, 1103, 100)
        ]
    )
    with pytest.raises(RuntimeError, match="gap"):
        await receiver.reconcile(client, spool, {10}, safe)
    assert spool.checkpoint(10) == 2
    spool.close()


@pytest.mark.asyncio
async def test_transport_loss_marks_unready_and_wakes_waiter(tmp_path):
    spool = EventSpool(tmp_path / "events.sqlite3", 1, {10})
    connected = True
    client = SimpleNamespace(
        _sender=SimpleNamespace(_transport_connected=lambda: connected)
    )
    service = EventSocket(spool, lambda: receiver.transport_connected(client))
    service.ready = True
    server = await service.start(tmp_path / "events.sock")
    watcher = asyncio.create_task(
        receiver.watch_transport(client, service, interval=0.01)
    )
    try:
        waiter = asyncio.create_task(
            read_event_socket(str(tmp_path / "events.sock"), timeout_seconds=2)
        )
        await asyncio.sleep(0.05)
        connected = False
        assert not service.ready
        with pytest.raises(RuntimeError, match="transport_lost"):
            await watcher
        result = await asyncio.wait_for(waiter, 0.5)
        assert result["receiver_ready"] is False
        assert not receiver.transport_connected(SimpleNamespace())
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        server.close()
        await server.wait_closed()
        spool.close()
