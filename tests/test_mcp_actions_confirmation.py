import mcp_server_actions as actions
import pytest


class _FakeManager:
    async def create_private_group(
        self, title, users=None, about="", kind="basic", dry_run=False
    ):
        return {
            "success": True,
            "action": "create_private_group",
            "dry_run": dry_run,
            "title": title,
            "about": about,
            "kind": kind,
            "invitees": [
                {
                    "input": user,
                    "user_id": 1000 + index,
                    "username": str(user).lstrip("@"),
                }
                for index, user in enumerate(users or [])
            ],
            "group_id": 123456789 if not dry_run else None,
        }

    async def send_message(self, group, message_text):
        return True

    async def delete_messages(self, group, message_ids, revoke=True, dry_run=False):
        return {
            "success": True,
            "action": "delete_messages",
            "dry_run": dry_run,
            "target": group,
            "message_ids": list(message_ids),
            "message_count": len(message_ids),
            "revoke": revoke,
        }

    async def clear_history(
        self, group, max_id=0, revoke=True, just_clear=False, dry_run=False
    ):
        return {
            "success": True,
            "action": "clear_history",
            "dry_run": dry_run,
            "target": group,
            "max_id": max_id,
            "revoke": revoke,
            "just_clear": just_clear,
        }

    async def leave_dialog(self, group, dry_run=False):
        return {
            "success": True,
            "action": "leave_dialog",
            "dry_run": dry_run,
            "target": group,
            "dialog_type": "channel",
        }

    async def edit_message(self, group, message_id, new_text, dry_run=False):
        return {
            "success": True,
            "action": "edit_message",
            "dry_run": dry_run,
            "target": group,
            "message_id": message_id,
            "new_text_len": len(new_text),
        }

    async def forward_messages(
        self,
        from_group,
        to_group,
        message_ids,
        drop_author=False,
        drop_media_captions=False,
        dry_run=False,
    ):
        return {
            "success": True,
            "action": "forward_messages",
            "dry_run": dry_run,
            "from_target": from_group,
            "to_target": to_group,
            "message_ids": list(message_ids),
            "message_count": len(message_ids),
            "drop_author": drop_author,
            "drop_media_captions": drop_media_captions,
        }

    async def set_channel_comments_join_requirement(
        self, channel_identifier, join_required, dry_run=False
    ):
        return {
            "success": True,
            "action": "set_channel_comments_join_requirement",
            "dry_run": dry_run,
            "join_required": join_required,
            "channel_target": "@Matskevich",
            "linked_group_target": "@matskevich_chat",
            "linked_group_id": 5023804528,
            "linked_group_title": "Matskevich comments",
            "current_join_required": True if dry_run else join_required,
        }


class _FakeCtx:
    current_session = "test"

    async def get_manager(self):
        return _FakeManager()


@pytest.mark.asyncio
async def test_send_message_requires_exact_confirmation_text(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"test_target"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", False)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")

    result = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=False,
        confirm=True,
        confirmation_text="подтверждаю",
    )

    assert result["success"] is False
    assert "confirmation_text" in result["error"]


@pytest.mark.asyncio
async def test_create_private_group_requires_allowlisted_title(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"other_target"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")

    result = await actions.tg_create_private_group(
        title="email critical",
        users=["@hermess260408bot"],
        dry_run=True,
    )

    assert result["success"] is False
    assert "TG_ACTIONS_ALLOWED_GROUPS" in result["error"]


@pytest.mark.asyncio
async def test_create_private_group_runs_dry_run_then_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"email critical"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "APPROVAL_MIN_AGE_SEC", 0)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    preview = await actions.tg_create_private_group(
        title="email critical",
        users=["@hermess260408bot"],
        dry_run=True,
    )
    assert preview["success"] is True
    approval_code = preview.get("approval_code")
    assert approval_code

    created = await actions.tg_create_private_group(
        title="email critical",
        users=["@hermess260408bot"],
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )

    assert created["success"] is True
    assert created["group_id"] == 123456789
    assert created["invitees"][0]["input"] == "@hermess260408bot"


@pytest.mark.asyncio
async def test_send_message_requires_one_time_approval_code(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"test_target"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "APPROVAL_MIN_AGE_SEC", 0)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    preview = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=True,
        confirm=False,
    )
    assert preview["success"] is True
    approval_code = preview.get("approval_code")
    assert approval_code

    blocked = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
    )
    assert blocked["success"] is False
    assert "approval_code" in blocked["error"]

    sent = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )
    assert sent["success"] is True

    reused = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )
    assert reused["success"] is False
    assert "invalid or expired" in reused["error"]


@pytest.mark.asyncio
async def test_send_message_blocks_immediate_execute_after_dry_run(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"test_target"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "APPROVAL_MIN_AGE_SEC", 30)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    ts = {"now": 1000.0}
    monkeypatch.setattr(actions.time, "time", lambda: ts["now"])

    preview = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=True,
        confirm=False,
    )
    assert preview["success"] is True
    approval_code = preview.get("approval_code")
    assert approval_code
    assert preview.get("approval_execute_after_ts") == 1030

    blocked = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )
    assert blocked["success"] is False
    assert "too fresh right after dry_run" in blocked["error"]

    ts["now"] = 1031.0
    sent = await actions.tg_send_message(
        group="test_target",
        message_text="hello",
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )
    assert sent["success"] is True


@pytest.mark.asyncio
async def test_set_channel_comments_join_requirement_requires_allowlisted_linked_group(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"other_target"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    result = await actions.tg_set_channel_comments_join_requirement(
        channel="@Matskevich",
        join_required=False,
        dry_run=True,
    )

    assert result["success"] is False
    assert result["linked_group_target"] == "@matskevich_chat"
    assert "not in TG_ACTIONS_ALLOWED_GROUPS" in result["error"]


@pytest.mark.asyncio
async def test_set_channel_comments_join_requirement_executes_after_dry_run(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"matskevich_chat"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "APPROVAL_MIN_AGE_SEC", 0)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    preview = await actions.tg_set_channel_comments_join_requirement(
        channel="@Matskevich",
        join_required=False,
        dry_run=True,
    )
    assert preview["success"] is True
    approval_code = preview.get("approval_code")
    assert approval_code

    executed = await actions.tg_set_channel_comments_join_requirement(
        channel="@Matskevich",
        join_required=False,
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )

    assert executed["success"] is True
    assert executed["join_required"] is False
    assert executed["linked_group_target"] == "@matskevich_chat"


@pytest.mark.asyncio
async def test_delete_messages_requires_one_time_approval_code(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"test_target"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "APPROVAL_MIN_AGE_SEC", 0)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    preview = await actions.tg_delete_messages(
        group="test_target",
        message_ids=[101, 102],
        dry_run=True,
    )
    assert preview["success"] is True
    approval_code = preview.get("approval_code")
    assert approval_code

    executed = await actions.tg_delete_messages(
        group="test_target",
        message_ids=[101, 102],
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )

    assert executed["success"] is True
    assert executed["message_count"] == 2
    assert executed["revoke"] is True


@pytest.mark.asyncio
async def test_leave_dialog_requires_one_time_approval_code(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"test_channel"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "APPROVAL_MIN_AGE_SEC", 0)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    preview = await actions.tg_leave_dialog(group="test_channel", dry_run=True)
    assert preview["success"] is True
    approval_code = preview.get("approval_code")
    assert approval_code

    executed = await actions.tg_leave_dialog(
        group="test_channel",
        dry_run=False,
        confirm=True,
        confirmation_text="отправляй",
        approval_code=approval_code,
    )

    assert executed["success"] is True
    assert executed["dry_run"] is False
    assert executed["action"] == "leave_dialog"


@pytest.mark.asyncio
async def test_forward_messages_requires_allowlisted_source(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"target_only"})
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "отправляй")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", False)
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", False)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "ctx", _FakeCtx())

    blocked = await actions.tg_forward_messages(
        from_group="source_chat",
        to_group="target_only",
        message_ids=[7],
        dry_run=True,
    )

    assert blocked["success"] is False
    assert "not in TG_ACTIONS_ALLOWED_GROUPS" in blocked["error"]
