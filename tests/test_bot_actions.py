"""guarded bot workflows are tested without connecting to telegram."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp_server_actions as actions
import pytest

from tganalytics.domain.groups import GroupManager
from tganalytics.infra import tele_client
from tganalytics.infra.managed_bot import CreateManagedBotRequest


@pytest.fixture
def bot_context(monkeypatch, tmp_path):
    for key, value in {
        "SAFE_STARTUP_BLOCK_REASON": None,
        "ACTIONS_ENABLED": True,
        "REQUIRE_ALLOWLIST": True,
        "ALLOWED_TARGETS": {"example_bot"},
        "REQUIRE_CONFIRMATION_TEXT": True,
        "CONFIRMATION_PHRASE": "test-confirmation",
        "REQUIRE_APPROVAL_CODE": True,
        "APPROVAL_MIN_AGE_SEC": 0,
        "IDEMPOTENCY_ENABLED": True,
        "APPROVAL_FILE": tmp_path / "approvals.json",
        "IDEMPOTENCY_FILE": tmp_path / "idempotency.json",
    }.items():
        monkeypatch.setattr(actions, key, value)
    button = SimpleNamespace(text="go", data=b"go", url=None)
    message = SimpleNamespace(id=7, message="choose", buttons=[[button]])
    client = AsyncMock()
    client.get_messages.return_value = message
    client.return_value = SimpleNamespace(id=42, username="created_bot", bot=True)
    manager = SimpleNamespace(
        _resolve_target_entity=AsyncMock(return_value=SimpleNamespace(id=123)),
        resolve_username=AsyncMock(
            return_value={"id": 42, "username": "created_bot", "is_bot": True}
        ),
    )
    monkeypatch.setattr(
        actions,
        "ctx",
        SimpleNamespace(client=client, get_manager=AsyncMock(return_value=manager)),
    )
    calls = []

    async def safe(func, *args, **kwargs):
        calls.append((args, dict(kwargs)))
        kwargs.pop("operation_type", None)
        kwargs.pop("timeout", None)
        return await func(*args, **kwargs)

    monkeypatch.setattr(actions, "safe_call", safe)
    return client, manager, calls


def confirmation(preview):
    return dict(
        dry_run=False,
        confirm=True,
        confirmation_text="test-confirmation",
        approval_code=preview["approval_code"],
    )


@pytest.mark.asyncio
async def test_callback_dry_run_then_exact_approval_uses_limiter(bot_context):
    client, _, calls = bot_context
    args = dict(group="@example_bot", message_id=7, button_text="go")
    preview = await actions.tg_click_inline_button(**args)
    assert preview["success"]
    assert client.await_count == 0
    result = await actions.tg_click_inline_button(**args, **confirmation(preview))
    assert result["success"]
    assert client.await_count == 1
    assert calls[-1][1]["operation_type"] == "group_msg"
    assert type(client.call_args.args[0]).__name__ == "GetBotCallbackAnswerRequest"
    reused = await actions.tg_click_inline_button(**args, **confirmation(preview))
    assert not reused["success"]
    assert client.await_count == 1


@pytest.mark.asyncio
async def test_managed_bot_creation_preserves_unknown_outcome(bot_context):
    client, _, calls = bot_context
    args = dict(manager_bot="@example_bot", name="example", username="created_bot")
    preview = await actions.tg_create_managed_bot(**args)
    assert preview["success"]
    assert preview["via_deeplink"] is False

    async def rpc(request):
        if isinstance(request, CreateManagedBotRequest):
            raise RuntimeError("Could not find a matching Constructor ID")
        return True

    client.side_effect = rpc
    result = await actions.tg_create_managed_bot(**args, **confirmation(preview))
    assert not result["success"]
    assert result["outcome_unknown"]
    assert "created_bot" not in result
    assert result["observed_bot"]["username"] == "created_bot"
    assert calls[-1][1]["operation_type"] == "api"
    duplicate_preview = await actions.tg_create_managed_bot(**args)
    duplicate = await actions.tg_create_managed_bot(
        **args, **confirmation(duplicate_preview)
    )
    assert duplicate["duplicate_blocked"]


@pytest.mark.asyncio
async def test_bot_steps_changed_payload_cannot_reuse_approval(bot_context):
    client, _, calls = bot_context
    args = dict(
        group="@example_bot",
        steps=[{"type": "send", "text": "hello", "wait_after_sec": 0}],
    )
    preview = await actions.tg_run_bot_steps(**args)
    assert preview["success"]
    client.send_message.assert_not_awaited()
    changed = await actions.tg_run_bot_steps(
        group="@example_bot",
        steps=[{"type": "send", "text": "changed"}],
        **confirmation(preview)
    )
    assert not changed["success"]
    client.send_message.assert_not_awaited()
    fresh = await actions.tg_run_bot_steps(**args)
    sent = await actions.tg_run_bot_steps(**args, **confirmation(fresh))
    assert sent["success"]
    client.send_message.assert_awaited_once()
    assert calls[-1][1]["operation_type"] == "group_msg"


@pytest.mark.asyncio
async def test_bot_tools_reject_targets_outside_allowlist(bot_context):
    client, _, calls = bot_context
    results = [
        await actions.tg_click_inline_button(group="@other_bot", message_id=7),
        await actions.tg_create_managed_bot(
            manager_bot="@other_bot", name="example", username="created_bot"
        ),
        await actions.tg_run_bot_steps(group="@other_bot", steps=[{"type": "wait"}]),
    ]
    assert all(not result["success"] for result in results)
    assert not calls
    client.assert_not_awaited()


@pytest.mark.asyncio
async def test_pin_and_comment_setting_use_rate_limiter(monkeypatch):
    client = AsyncMock()
    manager = GroupManager(client)
    entity = SimpleNamespace(id=123)
    monkeypatch.setattr(
        manager, "_resolve_target_entity", AsyncMock(return_value=entity)
    )
    monkeypatch.setattr(
        manager,
        "_resolve_linked_discussion_group",
        AsyncMock(return_value=(entity, {})),
    )
    safe = AsyncMock(return_value=True)
    monkeypatch.setattr("tganalytics.domain.groups._safe_api_call", safe)
    assert await manager.pin_message("@example", 7)
    assert safe.call_args.kwargs["operation_type"] == "group_msg"
    safe.reset_mock()
    preview = await manager.set_channel_comments_join_requirement(
        "@example", False, dry_run=True
    )
    assert preview["success"]
    safe.assert_not_awaited()
    result = await manager.set_channel_comments_join_requirement("@example", False)
    assert result["success"]
    request = safe.call_args.args[1]
    assert type(request).__name__ == "ToggleJoinToSendRequest"
    assert safe.call_args.kwargs["operation_type"] == "api"
    assert tele_client._is_telethon_write_request(request)
