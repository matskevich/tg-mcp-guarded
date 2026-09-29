import mcp_server_actions as actions
import pytest


class _LaneFakeManager:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, group, message_text):
        self.sent.append((group, message_text))
        return True


class _LaneFakeCtx:
    def __init__(
        self,
        account_id=16643982,
        username="test_operator",
        authorized=True,
    ):
        self.account_id = account_id
        self.username = username
        self.authorized = authorized
        self.manager = _LaneFakeManager()

    async def auth_status(self):
        if not self.authorized:
            return {"authorized": False, "error": "session unavailable"}
        return {
            "authorized": True,
            "account": {"id": self.account_id, "username": self.username},
        }

    async def get_manager(self):
        return self.manager


def _configure(monkeypatch, tmp_path, *, allowed_targets=None, ctx=None):
    monkeypatch.setattr(actions, "SAFE_STARTUP_BLOCK_REASON", None)
    monkeypatch.setattr(actions, "ACTIONS_ENABLED", True)
    monkeypatch.setattr(actions, "REQUIRE_ALLOWLIST", True)
    monkeypatch.setattr(
        actions,
        "ALLOWED_TARGETS",
        set(allowed_targets or {"field_helper", "site_team"}),
    )
    monkeypatch.setattr(actions, "REQUIRE_CONFIRMATION_TEXT", True)
    monkeypatch.setattr(actions, "CONFIRMATION_PHRASE", "confirm-test-action")
    monkeypatch.setattr(actions, "REQUIRE_APPROVAL_CODE", True)
    monkeypatch.setattr(actions, "APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "APPROVAL_MIN_AGE_SEC", 0)
    monkeypatch.setattr(actions, "APPROVAL_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(actions, "IDEMPOTENCY_ENABLED", True)
    monkeypatch.setattr(actions, "IDEMPOTENCY_WINDOW_SEC", 86400)
    monkeypatch.setattr(actions, "IDEMPOTENCY_FILE", tmp_path / "idempotency.json")
    monkeypatch.setattr(actions, "LANE_FILE", tmp_path / "lanes.json")
    monkeypatch.setattr(actions, "LANE_MAX_TTL_SEC", 86400)
    monkeypatch.setattr(actions, "LANE_APPROVAL_TTL_SEC", 1800)
    monkeypatch.setattr(actions, "LANE_MAX_TARGETS", 5)
    monkeypatch.setattr(actions, "LANE_MAX_MESSAGES", 20)
    monkeypatch.setattr(actions, "LANE_MIN_INTERVAL_SEC", 30)
    monkeypatch.setattr(actions, "LANE_SEND_LOCK_SEC", 120)
    monkeypatch.setattr(actions, "LANE_AUDIT_MAX_RECORDS", 50)
    monkeypatch.setattr(actions, "ctx", ctx or _LaneFakeCtx())


async def _create_lane(**overrides):
    payload = {
        "name": "equipment field survey",
        "purpose": "ask short follow-up questions about photographed equipment",
        "targets": ["@field_helper"],
        "ttl_sec": 86400,
        "max_messages": 5,
        "max_messages_per_target": 5,
        "min_interval_sec": 60,
        "max_message_len": 500,
        "allow_links": False,
        "active_hours_start": "",
        "active_hours_end": "",
        "active_timezone": "",
    }
    payload.update(overrides)
    return await actions.tg_create_write_lane(**payload)


@pytest.mark.asyncio
async def test_write_lane_requires_allowlisted_targets(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, allowed_targets={"field_helper"})

    result = await _create_lane(targets=["@not_allowed"])

    assert result["success"] is False
    assert "TG_ACTIONS_ALLOWED_GROUPS" in result["error"]


@pytest.mark.asyncio
async def test_write_lane_scope_caps_fail_closed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    too_long = await _create_lane(ttl_sec=86401)
    too_many = await _create_lane(max_messages=21)
    too_fast = await _create_lane(min_interval_sec=29)

    assert too_long["success"] is False
    assert "ttl_sec exceeds" in too_long["error"]
    assert too_many["success"] is False
    assert "max_messages exceeds" in too_many["error"]
    assert too_fast["success"] is False
    assert "below policy minimum" in too_fast["error"]


@pytest.mark.asyncio
async def test_write_lane_active_hours_are_approval_bound(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    preview = await _create_lane(
        active_hours_start="06:00",
        active_hours_end="21:00",
        active_timezone="Asia/Makassar",
    )

    assert preview["success"] is True
    assert preview["active_hours_start"] == "06:00"
    assert preview["active_hours_end"] == "21:00"
    assert preview["active_timezone"] == "Asia/Makassar"


@pytest.mark.asyncio
async def test_write_lane_rejects_invalid_active_hours(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    missing_end = await _create_lane(
        active_hours_start="06:00", active_timezone="Asia/Makassar"
    )
    bad_timezone = await _create_lane(
        active_hours_start="06:00",
        active_hours_end="21:00",
        active_timezone="Mars/Olympus",
    )

    assert missing_end["success"] is False
    assert "provided together" in missing_end["error"]
    assert bad_timezone["success"] is False
    assert "valid IANA timezone" in bad_timezone["error"]


def test_write_lane_active_hours_block_outside_window(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    lane = {
        "id": "lane_hours",
        "name": "survey",
        "purpose": "equipment follow-up",
        "status": "active",
        "approved": True,
        "targets": ["field_helper"],
        "ttl_sec": 86400,
        "max_messages": 5,
        "max_messages_per_target": 5,
        "min_interval_sec": 30,
        "max_message_len": 500,
        "allow_links": False,
        "active_hours_start": "06:00",
        "active_hours_end": "21:00",
        "active_timezone": "Asia/Makassar",
        "created_at_ts": 100,
        "approval_deadline_ts": 200,
        "expires_at_ts": 4102444800,
        "sent_count": 0,
        "target_sent_counts": {"field_helper": 0},
        "last_sent_at_by_target": {},
        "send_lock_token": None,
        "send_lock_until_ts": None,
        "audit": [],
    }
    lane["scope_hash"] = actions.lane_scope_hash(lane)
    actions._save_lanes_state({"lane_hours": lane})

    # 2026-08-20 22:00:00 Asia/Makassar.
    outside_ts = 1787234400
    token, _, error = actions._acquire_lane_send(
        "lane_hours", target="@field_helper", now_ts=outside_ts
    )

    assert token is None
    assert "outside approved active hours" in str(error)


@pytest.mark.asyncio
async def test_write_lane_approval_binds_scope_and_account(monkeypatch, tmp_path):
    ctx = _LaneFakeCtx(account_id=101, username="owner")
    _configure(monkeypatch, tmp_path, ctx=ctx)
    preview = await _create_lane()

    assert preview["success"] is True
    assert preview["status"] == "pending_approval"
    assert preview["approval_code"]

    wrong = await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="wrong phrase",
        approval_code=preview["approval_code"],
    )
    assert wrong["success"] is False
    assert "confirmation_text" in wrong["error"]

    approved = await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="confirm-test-action",
        approval_code=preview["approval_code"],
    )
    assert approved["success"] is True
    assert approved["status"] == "active"
    assert approved["bound_username"] == "owner"
    assert approved["delegated_actions"] == ["send_message"]


@pytest.mark.asyncio
async def test_write_lane_auth_failure_does_not_consume_approval(monkeypatch, tmp_path):
    ctx = _LaneFakeCtx(authorized=False)
    _configure(monkeypatch, tmp_path, ctx=ctx)
    preview = await _create_lane()

    blocked = await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="confirm-test-action",
        approval_code=preview["approval_code"],
    )
    ctx.authorized = True
    approved = await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="confirm-test-action",
        approval_code=preview["approval_code"],
    )

    assert blocked["success"] is False
    assert "session unavailable" in blocked["error"]
    assert approved["success"] is True


@pytest.mark.asyncio
async def test_write_lane_sends_without_per_message_confirmation(monkeypatch, tmp_path):
    ctx = _LaneFakeCtx()
    _configure(monkeypatch, tmp_path, ctx=ctx)
    preview = await _create_lane()
    approved = await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="confirm-test-action",
        approval_code=preview["approval_code"],
    )
    assert approved["success"] is True

    sent = await actions.tg_send_message_with_lane(
        preview["lane_id"],
        group="@field_helper",
        message_text="one close photo please",
        dry_run=False,
    )

    assert sent["success"] is True
    assert sent["lane"]["sent_count"] == 1
    assert ctx.manager.sent == [("@field_helper", "one close photo please")]

    status = await actions.tg_get_write_lane(preview["lane_id"], include_audit=True)
    assert status["success"] is True
    assert status["audit"][-1]["outcome"] == "sent"
    assert "message_text" not in status["audit"][-1]


@pytest.mark.asyncio
async def test_write_lane_reserves_idempotency_if_finalize_fails(monkeypatch, tmp_path):
    ctx = _LaneFakeCtx()
    _configure(monkeypatch, tmp_path, ctx=ctx)
    preview = await _create_lane()
    await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="confirm-test-action",
        approval_code=preview["approval_code"],
    )
    monkeypatch.setattr(
        actions,
        "_finalize_lane_send",
        lambda *args, **kwargs: (None, "lane state persistence failed"),
    )

    result = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@field_helper", "sent once", dry_run=False
    )
    action_hash = actions._hash_payload(
        {"action": "send_message", "target": "field_helper", "text": "sent once"}
    )
    duplicate, _ = actions._check_recent_duplicate(action_hash)

    assert result["success"] is False
    assert result["telegram_send_result"] is True
    assert duplicate is True


@pytest.mark.asyncio
async def test_write_lane_blocks_scope_links_and_account_drift(monkeypatch, tmp_path):
    ctx = _LaneFakeCtx(account_id=101)
    _configure(monkeypatch, tmp_path, ctx=ctx)
    preview = await _create_lane()
    await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="confirm-test-action",
        approval_code=preview["approval_code"],
    )

    outside = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@site_team", "hello", dry_run=False
    )
    linked = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@field_helper", "open https://example.com", dry_run=False
    )
    bare_link = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@field_helper", "open example.com", dry_run=False
    )
    ctx.account_id = 202
    drifted = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@field_helper", "hello", dry_run=False
    )

    assert outside["success"] is False
    assert "outside" in outside["error"]
    assert linked["success"] is False
    assert "does not allow links" in linked["error"]
    assert bare_link["success"] is False
    assert "does not allow links" in bare_link["error"]
    assert drifted["success"] is False
    assert "lane-bound account" in drifted["error"]
    assert ctx.manager.sent == []


@pytest.mark.asyncio
async def test_write_lane_quota_expiry_and_revoke(monkeypatch, tmp_path):
    ctx = _LaneFakeCtx()
    _configure(monkeypatch, tmp_path, ctx=ctx)
    preview = await _create_lane(max_messages=1, max_messages_per_target=1)
    await actions.tg_approve_write_lane(
        preview["lane_id"],
        confirmation_text="confirm-test-action",
        approval_code=preview["approval_code"],
    )

    first = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@field_helper", "first", dry_run=False
    )
    second = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@field_helper", "second", dry_run=False
    )
    revoked = await actions.tg_revoke_write_lane(preview["lane_id"], "done")
    after_revoke = await actions.tg_send_message_with_lane(
        preview["lane_id"], "@field_helper", "third", dry_run=False
    )

    assert first["success"] is True
    assert first["lane"]["status"] == "exhausted"
    assert second["success"] is False
    assert "not active" in second["error"]
    assert revoked["success"] is True
    assert revoked["status"] == "revoked"
    assert after_revoke["success"] is False


def test_write_lane_send_lock_blocks_parallel_workers(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    lane = {
        "id": "lane_test",
        "status": "active",
        "approved": True,
        "targets": ["field_helper"],
        "expires_at_ts": 1000,
        "max_messages": 5,
        "max_messages_per_target": 5,
        "min_interval_sec": 30,
        "sent_count": 0,
        "target_sent_counts": {"field_helper": 0},
        "last_sent_at_by_target": {},
        "send_lock_token": None,
        "send_lock_until_ts": None,
        "audit": [],
    }
    lane["scope_hash"] = actions.lane_scope_hash(lane)
    actions._save_lanes_state({"lane_test": lane})

    token, _, error = actions._acquire_lane_send(
        "lane_test", target="@field_helper", now_ts=100
    )
    second_token, _, second_error = actions._acquire_lane_send(
        "lane_test", target="@field_helper", now_ts=101
    )

    assert token
    assert error is None
    assert second_token is None
    assert "locked" in str(second_error)


def test_write_lane_pending_approval_expires_closed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    lane = {
        "id": "lane_pending",
        "status": "pending_approval",
        "approved": False,
        "approval_deadline_ts": 99,
        "audit": [],
    }
    lane["scope_hash"] = actions.lane_scope_hash(lane)
    actions._save_lanes_state({"lane_pending": lane})

    stored, error = actions._get_write_lane("lane_pending")

    assert error is None
    assert stored["status"] == "expired"
    assert stored["approved"] is False


def test_write_lane_min_interval_is_enforced(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    lane = {
        "id": "lane_interval",
        "status": "active",
        "approved": True,
        "targets": ["field_helper"],
        "expires_at_ts": 1000,
        "max_messages": 5,
        "max_messages_per_target": 5,
        "min_interval_sec": 60,
        "sent_count": 1,
        "target_sent_counts": {"field_helper": 1},
        "last_sent_at_by_target": {"field_helper": 100},
        "send_lock_token": None,
        "send_lock_until_ts": None,
        "audit": [],
    }
    lane["scope_hash"] = actions.lane_scope_hash(lane)
    actions._save_lanes_state({"lane_interval": lane})

    token, _, error = actions._acquire_lane_send(
        "lane_interval", target="@field_helper", now_ts=120
    )

    assert token is None
    assert "retry after 40s" in str(error)


@pytest.mark.asyncio
async def test_write_lane_revoke_preserves_inflight_audit(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    lane = {
        "id": "lane_inflight",
        "status": "active",
        "approved": True,
        "targets": ["field_helper"],
        "expires_at_ts": 4102444800,
        "max_messages": 5,
        "max_messages_per_target": 5,
        "min_interval_sec": 30,
        "sent_count": 0,
        "target_sent_counts": {"field_helper": 0},
        "last_sent_at_by_target": {},
        "send_lock_token": "token",
        "send_lock_until_ts": 4102444800,
        "audit": [],
    }
    lane["scope_hash"] = actions.lane_scope_hash(lane)
    actions._save_lanes_state({"lane_inflight": lane})

    revoked = await actions.tg_revoke_write_lane("lane_inflight", "stop")
    finalized, error = actions._finalize_lane_send(
        "lane_inflight",
        lock_token="token",
        target="@field_helper",
        action_hash="hash",
        message_len=5,
        success=True,
    )

    assert revoked["status"] == "revoked"
    assert error is None
    assert finalized["status"] == "revoked"
    assert finalized["sent_count"] == 1
    assert finalized["audit"][-1]["outcome"] == "sent"


def test_write_lane_scope_tampering_fails_closed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    lane = {
        "id": "lane_tampered",
        "name": "survey",
        "purpose": "equipment follow-up",
        "status": "active",
        "approved": True,
        "targets": ["field_helper"],
        "ttl_sec": 3600,
        "max_messages": 5,
        "max_messages_per_target": 5,
        "min_interval_sec": 60,
        "max_message_len": 500,
        "allow_links": False,
        "created_at_ts": 100,
        "approval_deadline_ts": 200,
        "expires_at_ts": 4102444800,
        "sent_count": 0,
        "target_sent_counts": {"field_helper": 0},
        "audit": [],
    }
    lane["scope_hash"] = actions.lane_scope_hash(lane)
    lane["targets"].append("site_team")
    actions._save_lanes_state({"lane_tampered": lane})

    stored, error = actions._get_write_lane("lane_tampered")

    assert error is None
    assert stored["status"] == "invalid_scope"
    assert stored["approved"] is False
