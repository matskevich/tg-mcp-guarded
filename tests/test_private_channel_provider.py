"""Shared delivery forwards exact approvals into the existing action runtime."""
from unittest.mock import MagicMock

import pytest
import mcp_private_channel_provider as provider


@pytest.fixture
def native(monkeypatch):
    client = MagicMock()
    def call(name, arguments):
        if name == "tg_auth_status":
            return {"authorized": True, "account": {"id": 42, "username": "dmatskevich"}}
        if name == "tg_get_actions_policy":
            return {**dict.fromkeys(("actions_enabled", "require_allowlist",
                "require_confirmation_text", "require_approval_code", "idempotency_enabled",
                "enforce_action_process", "direct_telethon_write_guard"), True),
                "allowed_targets": ["four"], "unsafe_override": False}
        return {"success": True, "dry_run": arguments["dry_run"]}
    client.call_tool.side_effect = call
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    monkeypatch.setattr(provider, "MCPStdioClient", factory)
    return client, factory


@pytest.mark.asyncio
async def test_preview_stays_preview_and_scopes_after_injection(native):
    client, factory = native
    result = await provider.tg_create_private_channel("four", 42)
    assert result["dry_run"]
    args = client.call_tool.call_args.args
    assert args == ("tg_create_private_channel", {"title": "four", "expected_actor_id": 42,
        "dry_run": True, "confirm": False, "confirmation_text": "", "approval_code": ""})
    spec = factory.call_args.args[0]
    assert spec.args.index("TG_ACTIONS_ALLOWED_GROUPS=four") > spec.args.index("--")
    assert "TG_ACTIONS_REQUIRE_ALLOWLIST=0" not in spec.args


@pytest.mark.asyncio
async def test_execution_preserves_approval(native):
    client, _ = native
    result = await provider.tg_create_private_channel("four", 42, dry_run=False,
        confirm=True, confirmation_text="exact phrase", approval_code="one-time")
    assert result["success"] and not result["dry_run"]
    assert client.call_tool.call_args.args[1]["approval_code"] == "one-time"
    assert client.call_tool.call_args.args[1]["confirmation_text"] == "exact phrase"


@pytest.mark.asyncio
async def test_actor_mismatch_stops_before_write(native):
    client, _ = native
    assert not (await provider.tg_create_private_channel("four", 99))["success"]
    assert [c.args[0] for c in client.call_tool.call_args_list] == ["tg_auth_status"]


@pytest.mark.asyncio
async def test_weakened_guard_stops_before_write(native):
    client, _ = native
    original = client.call_tool.side_effect
    def weak(name, args):
        result = original(name, args)
        if name == "tg_get_actions_policy":
            result["require_approval_code"] = False
        return result
    client.call_tool.side_effect = weak
    assert not (await provider.tg_create_private_channel("four", 42))["success"]
    assert "tg_create_private_channel" not in [c.args[0] for c in client.call_tool.call_args_list]


@pytest.mark.asyncio
async def test_status_has_no_session_paths(native):
    result = await provider.tg_private_channel_status()
    assert set(result) == {"authorized", "account"}


def test_adapter_does_not_offer_arbitrary_native_calls(native):
    with pytest.raises(ValueError):
        provider.invoke_native("tg_send_message", {})
