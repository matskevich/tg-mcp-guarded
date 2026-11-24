from types import SimpleNamespace
from unittest.mock import AsyncMock
import os

import pytest
import mcp_server_actions as actions


@pytest.fixture
def document_actor(monkeypatch, tmp_path):
    for key, value in {
        "SAFE_STARTUP_BLOCK_REASON": None,
        "ACTIONS_ENABLED": True,
        "REQUIRE_ALLOWLIST": True,
        "ALLOWED_TARGETS": {"12345"},
        "REQUIRE_CONFIRMATION_TEXT": True,
        "CONFIRMATION_PHRASE": "confirm-document",
        "REQUIRE_APPROVAL_CODE": True,
        "IDEMPOTENCY_ENABLED": False,
        "APPROVAL_MIN_AGE_SEC": 0,
        "APPROVAL_TTL_SEC": 1800,
        "APPROVAL_FILE": tmp_path / "approvals.json",
    }.items():
        monkeypatch.setattr(actions, key, value)
    manager = SimpleNamespace(send_file=AsyncMock(return_value=True))
    monkeypatch.setattr(actions, "ctx", SimpleNamespace(get_manager=AsyncMock(return_value=manager)))
    source = tmp_path / "13.jpg"
    source.write_bytes(b"unchanged original bytes")
    return source, manager


@pytest.mark.asyncio
async def test_document_mode_is_bound_to_approval(document_actor):
    source, manager = document_actor
    args = {"group": "12345", "file_path": str(source)}
    auto_preview = await actions.tg_send_file(**args, dry_run=True)
    blocked = await actions.tg_send_file(
        **args, force_document=True, confirm=True,
        confirmation_text="confirm-document", approval_code=auto_preview["approval_code"])
    assert blocked["success"] is False
    manager.send_file.assert_not_called()
    preview = await actions.tg_send_file(**args, force_document=True, dry_run=True)
    assert preview["force_document"] is True
    assert preview["action_hash"] != auto_preview["action_hash"]
    result = await actions.tg_send_file(
        **args, force_document=True, confirm=True,
        confirmation_text="confirm-document", approval_code=preview["approval_code"])
    assert result["success"] is True
    manager.send_file.assert_awaited_once_with("12345", str(source), caption="", force_document=True)
    reused = await actions.tg_send_file(
        **args, force_document=True, confirm=True,
        confirmation_text="confirm-document", approval_code=preview["approval_code"])
    assert reused["success"] is False
    assert source.read_bytes() == b"unchanged original bytes"


@pytest.mark.asyncio
async def test_default_mode_preserves_existing_approval_hash(document_actor):
    source, _ = document_actor
    preview = await actions.tg_send_file(group="12345", file_path=str(source), dry_run=True)
    stat = source.stat()
    legacy = actions._hash_payload({
        "action":"send_file", "target":"12345", "file_path":os.path.abspath(source),
        "file_size":stat.st_size, "file_mtime_ns":stat.st_mtime_ns, "caption":""})
    assert preview["action_hash"] == legacy


@pytest.mark.asyncio
async def test_document_mode_rejects_non_boolean(document_actor):
    source, manager = document_actor
    result = await actions.tg_send_file(group="12345", file_path=str(source),
                                        force_document="true", dry_run=True)
    assert result["success"] is False
    manager.send_file.assert_not_called()
