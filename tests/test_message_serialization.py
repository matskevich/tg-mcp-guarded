"""read and action tools must agree on button selectors and channel messages."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import Message, PeerChannel, PeerUser

from tganalytics.domain.bot_workflow import (
    message_button_options,
    public_button_options,
)
from tganalytics.domain.groups import GroupManager


def test_button_records_match_read_preview_and_hide_live_objects():
    buttons = [
        SimpleNamespace(text="go", data=b"\x00\xff", url=None),
        SimpleNamespace(text="docs", data=None, url="https://example.com"),
    ]
    msg = SimpleNamespace(buttons=[buttons])
    options = message_button_options(msg)
    public = public_button_options(options)
    assert GroupManager._message_buttons_to_records(msg) == public
    assert options[0]["_button"] is buttons[0]
    assert all("_button" not in item for item in public)
    assert public[0]["data_b64"] == "AP8="
    assert public[0]["is_callback"] is True
    assert public[1]["is_callback"] is False


@pytest.mark.parametrize(
    "peer, sender", [(PeerChannel(123), -1000000000123), (PeerUser(42), 42)]
)
def test_message_record_handles_channel_and_user_senders(peer, sender):
    msg = Message(
        id=7,
        peer_id=peer,
        from_id=peer,
        message="hello",
        date=datetime.now(timezone.utc),
    )
    record = GroupManager._message_to_record(msg)
    assert record["from_id"] == sender
    assert record["text"] == record["caption"] == "hello"


@pytest.mark.asyncio
async def test_history_uses_shared_record_and_preserves_both_pagination_bounds(
    monkeypatch,
):
    msg = Message(
        id=7,
        peer_id=PeerChannel(123),
        from_id=PeerChannel(123),
        message="post",
        date=datetime.now(timezone.utc),
    )
    observed = {}

    async def iterate(entity, **kwargs):
        observed.update(kwargs)
        yield msg

    async def call(func, *args, **kwargs):
        return await func(*args, **kwargs)

    client = SimpleNamespace(iter_messages=iterate)
    manager = GroupManager(client)
    monkeypatch.setattr(
        manager, "_resolve_read_entity", AsyncMock(return_value=msg.peer_id)
    )
    monkeypatch.setattr("tganalytics.domain.groups._safe_api_call", call)
    records = await manager.get_messages("channel", limit=10, max_id=8, offset_id=12)
    assert records == [GroupManager._message_to_record(msg)]
    assert observed["max_id"] == 8
    assert observed["offset_id"] == 12
