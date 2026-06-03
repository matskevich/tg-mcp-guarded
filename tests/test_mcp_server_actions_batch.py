import json
import os

import pytest

os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "testhash")

import mcp_server_actions as actions  # noqa: E402


class _BatchFakeManager:
    async def delete_messages(self, group, message_ids, revoke=True, dry_run=False):
        return {
            "success": True,
            "action": "delete_messages",
            "target": group,
            "message_ids": list(message_ids),
            "message_count": len(message_ids),
            "revoke": revoke,
            "dry_run": dry_run,
        }

    async def leave_dialog(self, group, dry_run=False):
        return {
            "success": True,
            "action": "leave_dialog",
            "target": group,
            "dry_run": dry_run,
        }


class _BatchFakeCtx:
    async def get_manager(self):
        return _BatchFakeManager()


def test_create_add_member_batch_record_dedup_and_policy(monkeypatch):
    monkeypatch.setattr(
        actions,
        "_check_target_allowed",
        lambda group: (False, "blocked") if group == "bad_group" else (True, None),
    )

    batch, blocked = actions._create_add_member_batch_record(
        user="new_user",
        groups=["g1", "g1", "bad_group"],
        note="test",
        ttl_hours=24,
    )

    assert batch["type"] == "add_member"
    assert batch["approved"] is False
    assert batch["approved_until_ts"] is None
    assert len(batch["actions"]) == 2
    assert len(blocked) == 1
    assert blocked[0]["group"] == "bad_group"

    statuses = {a["group"]: a["status"] for a in batch["actions"]}
    assert statuses["g1"] == "pending"
    assert statuses["bad_group"] == "blocked_policy"


@pytest.mark.asyncio
async def test_batch_approval_expiry_requires_reapprove(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", False)
    monkeypatch.setattr(actions, "BATCH_FILE", tmp_path / "batches.json")
    monkeypatch.setattr(actions, "BATCH_APPROVAL_LEASE_SEC", 86400)
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", False)

    state = {
        "b1": {
            "id": "b1",
            "type": "add_member",
            "status": "approved",
            "approved": True,
            "approved_at_ts": 10,
            "approved_until_ts": 20,
            "created_at_ts": 1,
            "expires_at_ts": 9999999999,
            "user": "new_user",
            "actions": [{"group": "g1", "status": "pending", "attempts": 0}],
        }
    }
    actions._save_batches_state(state)

    monkeypatch.setattr(actions.time, "time", lambda: 30)

    result = await actions.tg_run_add_member_batch("b1", max_actions=1)
    assert result["success"] is False
    assert "approval expired" in result["error"]

    _, batch = actions._get_batch("b1")
    assert batch is not None
    assert batch["approved"] is False
    assert batch["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_batch_run_lock_blocks_parallel_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", False)
    monkeypatch.setattr(actions, "BATCH_FILE", tmp_path / "batches.json")
    monkeypatch.setattr(actions, "BATCH_RUN_LEASE_SEC", 1800)
    monkeypatch.setattr(actions.time, "time", lambda: 100)

    state = {
        "b1": {
            "id": "b1",
            "type": "add_member",
            "status": "approved",
            "approved": True,
            "approved_at_ts": 10,
            "approved_until_ts": 9999,
            "run_lock_owner": "other-worker",
            "run_lock_until_ts": 9999,
            "created_at_ts": 1,
            "expires_at_ts": 9999999999,
            "user": "new_user",
            "actions": [{"group": "g1", "status": "pending", "attempts": 0}],
        }
    }
    actions._save_batches_state(state)

    result = await actions.tg_run_add_member_batch("b1", max_actions=1)
    assert result["success"] is False
    assert "already running" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_create_delete_messages_batch_from_manifest_chunks_and_policy(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"good"})
    monkeypatch.setattr(actions, "BATCH_FILE", tmp_path / "batches.json")

    manifest = tmp_path / "delete_manifest.json"
    manifest.write_text(
        '{"targets":[{"group":"good","message_ids":[1,2,3]},{"group":"bad","message_ids":[4]}]}',
        encoding="utf-8",
    )

    result = await actions.tg_create_delete_messages_batch_from_manifest(
        str(manifest),
        max_ids_per_action=2,
    )

    assert result["success"] is True
    assert result["batch_type"] == "delete_messages"
    assert result["total"] == 3
    assert result["blocked_policy_count"] == 1


@pytest.mark.asyncio
async def test_run_delete_messages_batch_executes_approved_chunks(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", False)
    monkeypatch.setattr(actions, "BATCH_FILE", tmp_path / "batches.json")
    monkeypatch.setattr(actions, "BATCH_RUN_LEASE_SEC", 1800)
    monkeypatch.setattr(actions, "ctx", _BatchFakeCtx())
    monkeypatch.setattr(actions.time, "time", lambda: 100)

    state = {
        "b1": {
            "id": "b1",
            "type": "delete_messages",
            "status": "approved",
            "approved": True,
            "approved_at_ts": 10,
            "approved_until_ts": 9999,
            "run_lock_owner": None,
            "run_lock_until_ts": None,
            "created_at_ts": 1,
            "expires_at_ts": 9999999999,
            "revoke": True,
            "actions": [
                {
                    "group": "g1",
                    "message_ids": [1, 2],
                    "status": "pending",
                    "attempts": 0,
                }
            ],
        }
    }
    actions._save_batches_state(state)

    result = await actions.tg_run_delete_messages_batch("b1", max_actions=1)

    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["success_count"] == 1


@pytest.mark.asyncio
async def test_leave_dialog_batch_from_candidates_uses_reviewed_candidates(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(actions, "ALLOWED_TARGETS", {"-1001"})
    monkeypatch.setattr(actions, "BATCH_FILE", tmp_path / "batches.json")

    candidates = tmp_path / "channel_candidates.json"
    candidates.write_text(
        json.dumps(
            {
                "candidates": [
                    {"target": "-1001", "review": {"leave_candidate": True}},
                    {"target": "-1002", "review": {"leave_candidate": False}},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = await actions.tg_create_leave_dialog_batch_from_candidates(str(candidates))

    assert result["success"] is True
    assert result["batch_type"] == "leave_dialog"
    assert result["total"] == 1
