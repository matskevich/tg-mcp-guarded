"""Broadcast creation preserves account, approval and uncertain-effect fences."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import mcp_server_actions as actions
from mcp_private_channel import register_actions
from telethon.tl.types import Channel
from telethon.tl.functions.channels import CreateChannelRequest
from tganalytics.infra import tele_client


@pytest.fixture
def setup_channel(monkeypatch, tmp_path):
    for key, value in dict(SAFE_STARTUP_BLOCK_REASON=None, ACTIONS_ENABLED=True,
        REQUIRE_ALLOWLIST=True, ALLOWED_TARGETS={"four"}, REQUIRE_CONFIRMATION_TEXT=True,
        CONFIRMATION_PHRASE="confirm-test", REQUIRE_APPROVAL_CODE=True, APPROVAL_MIN_AGE_SEC=0,
        IDEMPOTENCY_ENABLED=True, APPROVAL_FILE=tmp_path/"a.json",
        IDEMPOTENCY_FILE=tmp_path/"i.json").items():
        monkeypatch.setattr(actions, key, value)
    channel = Channel(id=1234567890, title="four", photo=None,
        date=datetime.now(timezone.utc), creator=True, broadcast=True,
        megagroup=False, signatures=False)
    client = SimpleNamespace(get_me=AsyncMock(), get_entity=AsyncMock())
    async def call(fn, *args, **kwargs):
        if fn is client.get_me:
            return SimpleNamespace(id=42)
        if fn is client.get_entity:
            return channel
        return SimpleNamespace(chats=[channel])
    safe = AsyncMock(side_effect=call)
    monkeypatch.setattr(actions, "safe_call", safe)
    monkeypatch.setattr(actions, "ctx", SimpleNamespace(get_manager=AsyncMock(
        return_value=SimpleNamespace(client=client))))
    tool = register_actions(vars(actions))
    return tool, safe, channel


@pytest.mark.asyncio
async def test_creation_exact_approval_and_private_broadcast(setup_channel):
    tool, safe, channel = setup_channel
    args = dict(title="four", expected_actor_id=42)
    preview = await tool(**args)
    assert preview["dry_run"] and safe.await_count == 1
    denied = await tool(**{**args, "title": "other"}, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert not denied["success"] and safe.await_count == 1
    result = await tool(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert result["success"] and result["read_back_verified"]
    writes = [c for c in safe.await_args_list
              if len(c.args) > 1 and isinstance(c.args[1], CreateChannelRequest)]
    assert len(writes) == 1 and writes[0].args[1].broadcast
    assert not writes[0].args[1].megagroup and writes[0].kwargs["max_retries"] == 0
    assert (await tool(**args))["duplicate_blocked"]


@pytest.mark.asyncio
async def test_wrong_actor_does_not_create(setup_channel):
    tool, safe, _ = setup_channel
    assert not (await tool(title="four", expected_actor_id=99))["success"]
    assert safe.await_count == 1


@pytest.mark.asyncio
async def test_lost_creation_response_requires_reconciliation(setup_channel):
    tool, safe, _ = setup_channel
    args = dict(title="four", expected_actor_id=42)
    preview = await tool(**args)
    original = safe.side_effect
    async def lost(fn, *args, **kwargs):
        if args and isinstance(args[0], CreateChannelRequest):
            raise TimeoutError("response lost")
        return await original(fn, *args, **kwargs)
    safe.side_effect = lost
    result = await tool(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert result["reconciliation_required"] and result["submitted"]
    assert (await tool(**args))["duplicate_blocked"]


@pytest.mark.asyncio
async def test_signed_channel_fails_acceptance(setup_channel):
    tool, _, channel = setup_channel
    args = dict(title="four", expected_actor_id=42)
    preview = await tool(**args)
    channel.signatures = True
    result = await tool(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert not result["success"] and result["reconciliation_required"]


def test_creation_rpc_is_guarded():
    assert tele_client._is_telethon_write_request(
        CreateChannelRequest(title="four", about="", broadcast=True))
