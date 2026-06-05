"""Actions-focused MCP server for tganalytics.

Contains high-risk Telegram operations behind explicit env gates.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Hard default: direct telethon writes are blocked unless context is actions_mcp.
os.environ.setdefault("TG_BLOCK_DIRECT_TELETHON_WRITE", "1")
os.environ.setdefault("TG_ALLOW_DIRECT_TELETHON_WRITE", "0")
os.environ.setdefault("TG_ENFORCE_ACTION_PROCESS", "1")
os.environ.setdefault("TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS", "actions_mcp")
os.environ.setdefault("TG_WRITE_CONTEXT", "actions_mcp")
os.environ.setdefault("TG_ACTION_PROCESS", "1")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp_actions_batch import (  # noqa: E402
    create_add_member_batch_record,
    create_delete_messages_batch_record,
    create_leave_dialog_batch_record,
    summarize_batch,
)
from mcp_actions_policy import (  # noqa: E402
    detect_unsafe_defaults,
    hash_payload,
    normalize_target,
    parse_allowlist,
    validate_confirmation_text,
)
from mcp_actions_state import load_json_dict, update_json_dict  # noqa: E402
from mcp_server_common import MCPServerContext  # noqa: E402

from tganalytics.infra.limiter import get_rate_limiter  # noqa: E402
from tganalytics.infra.metrics import snapshot  # noqa: E402

SERVER_NAME = os.environ.get("TG_MCP_SERVER_NAME", "tganalytics-actions")
ALLOW_SESSION_SWITCH = os.environ.get("TG_ALLOW_SESSION_SWITCH", "0") == "1"
ACTIONS_ENABLED = os.environ.get("TG_ACTIONS_ENABLED", "0") == "1"
REQUIRE_ALLOWLIST = os.environ.get("TG_ACTIONS_REQUIRE_ALLOWLIST", "1") == "1"
UNSAFE_OVERRIDE = os.environ.get("TG_ACTIONS_UNSAFE_OVERRIDE", "0") == "1"

try:
    MAX_MESSAGE_LEN = int(os.environ.get("TG_ACTIONS_MAX_MESSAGE_LEN", "2000"))
except ValueError:
    MAX_MESSAGE_LEN = 2000

try:
    MAX_FILE_MB = int(os.environ.get("TG_ACTIONS_MAX_FILE_MB", "20"))
except ValueError:
    MAX_FILE_MB = 20

try:
    MIN_CONFIRMATION_TEXT_LEN = int(
        os.environ.get("TG_ACTIONS_MIN_CONFIRM_TEXT_LEN", "6")
    )
except ValueError:
    MIN_CONFIRMATION_TEXT_LEN = 6

try:
    IDEMPOTENCY_WINDOW_SEC = int(
        os.environ.get("TG_ACTIONS_IDEMPOTENCY_WINDOW_SEC", str(24 * 3600))
    )
except ValueError:
    IDEMPOTENCY_WINDOW_SEC = 24 * 3600

REQUIRE_CONFIRMATION_TEXT = (
    os.environ.get("TG_ACTIONS_REQUIRE_CONFIRMATION_TEXT", "1") == "1"
)
CONFIRMATION_PHRASE = os.environ.get("TG_ACTIONS_CONFIRMATION_PHRASE", "").strip().lower()
REQUIRE_APPROVAL_CODE = os.environ.get("TG_ACTIONS_REQUIRE_APPROVAL_CODE", "1") == "1"
IDEMPOTENCY_ENABLED = os.environ.get("TG_ACTIONS_IDEMPOTENCY_ENABLED", "1") == "1"
IDEMPOTENCY_FILE = Path(
    os.environ.get(
        "TG_ACTIONS_IDEMPOTENCY_FILE", "data/anti_spam/action_idempotency.json"
    )
)

try:
    APPROVAL_TTL_SEC = int(os.environ.get("TG_ACTIONS_APPROVAL_TTL_SEC", "1800"))
except ValueError:
    APPROVAL_TTL_SEC = 1800

try:
    APPROVAL_MIN_AGE_SEC = int(os.environ.get("TG_ACTIONS_APPROVAL_MIN_AGE_SEC", "30"))
except ValueError:
    APPROVAL_MIN_AGE_SEC = 30

APPROVAL_FILE = Path(
    os.environ.get("TG_ACTIONS_APPROVAL_FILE", "data/anti_spam/action_approvals.json")
)

try:
    BATCH_DEFAULT_TTL_HOURS = int(os.environ.get("TG_ACTIONS_BATCH_TTL_HOURS", "168"))
except ValueError:
    BATCH_DEFAULT_TTL_HOURS = 168

try:
    BATCH_APPROVAL_LEASE_SEC = int(
        os.environ.get("TG_ACTIONS_BATCH_APPROVAL_LEASE_SEC", str(24 * 3600))
    )
except ValueError:
    BATCH_APPROVAL_LEASE_SEC = 24 * 3600

try:
    BATCH_RUN_LEASE_SEC = int(os.environ.get("TG_ACTIONS_BATCH_RUN_LEASE_SEC", "1800"))
except ValueError:
    BATCH_RUN_LEASE_SEC = 1800

BATCH_FILE = Path(
    os.environ.get("TG_ACTIONS_BATCH_FILE", "data/anti_spam/action_batches.json")
)


def _detect_unsafe_defaults() -> list[str]:
    """Return list of unsafe policy settings."""
    return detect_unsafe_defaults(
        env=os.environ,
        require_allowlist=REQUIRE_ALLOWLIST,
        require_confirmation_text=REQUIRE_CONFIRMATION_TEXT,
        require_approval_code=REQUIRE_APPROVAL_CODE,
        idempotency_enabled=IDEMPOTENCY_ENABLED,
    )


UNSAFE_POLICY_ISSUES = _detect_unsafe_defaults()
SAFE_STARTUP_BLOCK_REASON = None
if UNSAFE_POLICY_ISSUES and not UNSAFE_OVERRIDE:
    ACTIONS_ENABLED = False
    SAFE_STARTUP_BLOCK_REASON = (
        "Unsafe ActionMCP policy detected: "
        + "; ".join(UNSAFE_POLICY_ISSUES)
        + ". Set TG_ACTIONS_UNSAFE_OVERRIDE=1 only if you really need non-safe mode."
    )


def _normalize_target(group: str) -> str:
    return normalize_target(group)


def _parse_allowlist(raw: str) -> set[str]:
    return parse_allowlist(raw)


ALLOWED_TARGETS = _parse_allowlist(os.environ.get("TG_ACTIONS_ALLOWED_GROUPS", ""))

mcp = FastMCP(SERVER_NAME)
ctx = MCPServerContext(
    allow_session_switch=ALLOW_SESSION_SWITCH, server_profile="actions"
)


def _check_target_allowed(group: str) -> tuple[bool, str | None]:
    normalized = _normalize_target(group)

    if REQUIRE_ALLOWLIST and not ALLOWED_TARGETS:
        return (
            False,
            "Actions blocked: TG_ACTIONS_REQUIRE_ALLOWLIST=1 but "
            "TG_ACTIONS_ALLOWED_GROUPS is empty.",
        )

    if ALLOWED_TARGETS and normalized not in ALLOWED_TARGETS:
        return (
            False,
            f"Target '{group}' is not in TG_ACTIONS_ALLOWED_GROUPS.",
        )

    return True, None


def _check_action_preconditions(
    group: str,
    dry_run: bool,
    confirm: bool,
    confirmation_text: str = "",
) -> tuple[bool, str | None]:
    if SAFE_STARTUP_BLOCK_REASON:
        return False, SAFE_STARTUP_BLOCK_REASON

    if not ACTIONS_ENABLED:
        return False, "Actions are disabled. Set TG_ACTIONS_ENABLED=1."

    allowed, error = _check_target_allowed(group)
    if not allowed:
        return False, error

    if not dry_run and not confirm:
        return (
            False,
            "Execution blocked: set confirm=true to run destructive action. "
            "Use dry_run=true to preview safely.",
        )

    ok, err = _validate_confirmation_text(confirmation_text, dry_run=dry_run)
    if not ok:
        return False, err

    return True, None


def _suggest_next_step(error: str | None) -> str | None:
    text = str(error or "").lower()
    if not text:
        return None
    if "unsafe actionmcp policy detected" in text:
        return (
            "Restore strict safety env flags, then restart ActionMCP. "
            "Use TG_ACTIONS_UNSAFE_OVERRIDE=1 only for temporary debugging."
        )
    if "actions are disabled" in text:
        return "Set TG_ACTIONS_ENABLED=1 for ActionMCP and restart server."
    if "require_allowlist=1 but tg_actions_allowed_groups is empty" in text:
        return (
            "Set TG_ACTIONS_ALLOWED_GROUPS with explicit targets, then retry dry_run."
        )
    if "is not in tg_actions_allowed_groups" in text:
        return "Add this target to TG_ACTIONS_ALLOWED_GROUPS, then retry dry_run."
    if "confirm=true" in text:
        return "Run same action with dry_run=true first, then rerun with confirm=true."
    if "confirmation_text" in text:
        return f"Use exact confirmation_text='{CONFIRMATION_PHRASE}' in this thread."
    if "too fresh right after dry_run" in text:
        return "Wait until approval min age passes, then execute with the same approval_code."
    if "approval_code" in text:
        return "Run matching action with dry_run=true to get one-time approval_code, then execute."
    if "duplicate action blocked" in text:
        return (
            "Wait until idempotency window expires or set force_resend=true "
            "if resend is intentional."
        )
    return None


def _blocked(error: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": False, "error": error}
    step = _suggest_next_step(error)
    if step:
        payload["next_step"] = step
    payload.update(extra)
    return payload


def _hash_payload(payload: dict[str, Any]) -> str:
    return hash_payload(payload)


def _normalize_message_ids_arg(message_ids: Any) -> list[int]:
    raw_items = (
        [message_ids] if isinstance(message_ids, int) else list(message_ids or [])
    )
    normalized: list[int] = []
    seen = set()
    for item in raw_items:
        try:
            message_id = int(item)
        except Exception as exc:
            raise ValueError(f"Invalid message id: {item!r}") from exc
        if message_id <= 0:
            raise ValueError(f"Invalid message id: {message_id}")
        if message_id in seen:
            continue
        seen.add(message_id)
        normalized.append(message_id)
    if not normalized:
        raise ValueError("message_ids is empty")
    return normalized


def _load_idempotency_state() -> dict[str, float]:
    raw = load_json_dict(IDEMPOTENCY_FILE)
    state: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(key, str):
            try:
                state[key] = float(value)
            except Exception:
                continue
    return state


def _save_idempotency_state(state: dict[str, float]) -> None:
    normalized = {}
    for key, value in state.items():
        if isinstance(key, str):
            try:
                normalized[key] = float(value)
            except Exception:
                continue

    def _mut(current: dict[str, Any]) -> None:
        current.clear()
        current.update(normalized)

    update_json_dict(IDEMPOTENCY_FILE, _mut)


def _check_recent_duplicate(
    action_hash: str, now_ts: float | None = None
) -> tuple[bool, int]:
    if not IDEMPOTENCY_ENABLED:
        return False, 0
    now = now_ts if now_ts is not None else time.time()

    def _mut(state: dict[str, Any]) -> tuple[bool, int]:
        normalized: dict[str, float] = {}
        for key, value in state.items():
            if not isinstance(key, str):
                continue
            try:
                normalized[key] = float(value)
            except Exception:
                continue

        # Trim stale keys while reading.
        fresh = {
            k: v for k, v in normalized.items() if (now - v) <= IDEMPOTENCY_WINDOW_SEC
        }
        state.clear()
        state.update(fresh)

        last_ts = fresh.get(action_hash)
        if last_ts is None:
            return False, 0

        retry_after = int(max(0, IDEMPOTENCY_WINDOW_SEC - (now - last_ts)))
        return retry_after > 0, retry_after

    return update_json_dict(IDEMPOTENCY_FILE, _mut)


def _mark_action_executed(action_hash: str, now_ts: float | None = None) -> None:
    if not IDEMPOTENCY_ENABLED:
        return
    now = now_ts if now_ts is not None else time.time()

    def _mut(state: dict[str, Any]) -> None:
        state[action_hash] = float(now)

    update_json_dict(IDEMPOTENCY_FILE, _mut)


def _validate_confirmation_text(
    confirmation_text: str, dry_run: bool
) -> tuple[bool, str | None]:
    return validate_confirmation_text(
        confirmation_text=confirmation_text,
        dry_run=dry_run,
        require_confirmation_text=REQUIRE_CONFIRMATION_TEXT,
        min_confirmation_text_len=MIN_CONFIRMATION_TEXT_LEN,
        confirmation_phrase=CONFIRMATION_PHRASE,
    )


def _load_approvals_state() -> dict[str, dict[str, Any]]:
    raw = load_json_dict(APPROVAL_FILE)
    state: dict[str, dict[str, Any]] = {}
    for code, item in raw.items():
        if not isinstance(code, str) or not isinstance(item, dict):
            continue
        digest = item.get("digest")
        expires_at = item.get("expires_at")
        if not isinstance(digest, str):
            continue
        try:
            exp = float(expires_at)
        except Exception:
            continue
        issued_at = item.get("issued_at")
        try:
            issued = float(issued_at) if issued_at is not None else 0.0
        except Exception:
            issued = 0.0
        state[code] = {"digest": digest, "expires_at": exp, "issued_at": issued}
    return state


def _save_approvals_state(state: dict[str, dict[str, Any]]) -> None:
    normalized: dict[str, dict[str, Any]] = {}
    for code, item in state.items():
        if not isinstance(code, str) or not isinstance(item, dict):
            continue
        digest = item.get("digest")
        expires_at = item.get("expires_at")
        if not isinstance(digest, str):
            continue
        try:
            exp = float(expires_at)
        except Exception:
            continue
        issued_at = item.get("issued_at")
        try:
            issued = float(issued_at) if issued_at is not None else 0.0
        except Exception:
            issued = 0.0
        normalized[code] = {"digest": digest, "expires_at": exp, "issued_at": issued}

    def _mut(current: dict[str, Any]) -> None:
        current.clear()
        current.update(normalized)

    update_json_dict(APPROVAL_FILE, _mut)


def _trim_approvals(
    state: dict[str, dict[str, Any]], now_ts: float | None = None
) -> dict[str, dict[str, Any]]:
    now = now_ts if now_ts is not None else time.time()
    return {
        code: item
        for code, item in state.items()
        if isinstance(item, dict) and float(item.get("expires_at", 0)) > now
    }


def _issue_approval(payload_hash: str, now_ts: float | None = None) -> dict[str, Any]:
    now = now_ts if now_ts is not None else time.time()
    code = secrets.token_urlsafe(9)
    expires_at = now + APPROVAL_TTL_SEC
    execute_after = now + max(0, APPROVAL_MIN_AGE_SEC)

    def _mut(state: dict[str, Any]) -> None:
        trimmed = _trim_approvals(state, now_ts=now)
        trimmed[code] = {
            "digest": payload_hash,
            "expires_at": expires_at,
            "issued_at": float(now),
        }
        state.clear()
        state.update(trimmed)

    update_json_dict(APPROVAL_FILE, _mut)
    return {
        "approval_code": code,
        "approval_expires_in_sec": APPROVAL_TTL_SEC,
        "approval_expires_at_ts": int(expires_at),
        "approval_min_age_sec": max(0, APPROVAL_MIN_AGE_SEC),
        "approval_execute_after_ts": int(execute_after),
    }


def _consume_approval(
    payload_hash: str, approval_code: str, now_ts: float | None = None
) -> tuple[bool, str | None]:
    now = now_ts if now_ts is not None else time.time()
    code = (approval_code or "").strip()

    def _mut(state: dict[str, Any]) -> tuple[bool, str | None]:
        trimmed = _trim_approvals(state, now_ts=now)
        state.clear()
        state.update(trimmed)

        if not code:
            return (
                False,
                "Execution blocked: approval_code is required. "
                "Run the same action with dry_run=true first.",
            )

        item = state.get(code)
        if not item:
            return False, "Execution blocked: approval_code is invalid or expired."
        if item.get("digest") != payload_hash:
            return (
                False,
                "Execution blocked: approval_code does not match this payload. "
                "Generate a fresh dry_run preview.",
            )
        issued_at = float(item.get("issued_at") or 0.0)
        earliest_exec_ts = issued_at + max(0, APPROVAL_MIN_AGE_SEC)
        if now < earliest_exec_ts:
            wait_sec = int(max(1, earliest_exec_ts - now))
            return (
                False,
                "Execution blocked: approval_code is too fresh right after dry_run. "
                f"Wait {wait_sec}s and ask human to verify preview before execute.",
            )

        state.pop(code, None)
        return True, None

    return update_json_dict(APPROVAL_FILE, _mut)


def _approval_gate(
    *,
    action_hash: str,
    dry_run: bool,
    approval_code: str,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    if not REQUIRE_APPROVAL_CODE:
        return True, None, None
    if dry_run:
        return True, None, _issue_approval(action_hash)
    ok, err = _consume_approval(action_hash, approval_code)
    return ok, err, None


def _load_batches_state() -> dict[str, dict[str, Any]]:
    batches = load_json_dict(BATCH_FILE, root_key="batches")
    return {str(k): v for k, v in batches.items() if isinstance(v, dict)}


def _save_batches_state(state: dict[str, dict[str, Any]]) -> None:
    normalized = {str(k): v for k, v in state.items() if isinstance(v, dict)}

    def _mut(current: dict[str, Any]) -> None:
        current.clear()
        current.update(normalized)

    update_json_dict(BATCH_FILE, _mut, root_key="batches")


def _get_batch(
    batch_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    state = _load_batches_state()
    batch = state.get((batch_id or "").strip())
    return state, batch


def _batch_run_owner() -> str:
    return f"{SERVER_NAME}:{os.getpid()}"


def _acquire_batch_run_lock(
    batch_id: str, now_ts: int | None = None
) -> tuple[bool, str | None]:
    now = int(now_ts if now_ts is not None else time.time())
    owner = _batch_run_owner()
    bid = (batch_id or "").strip()
    blocked_error: str | None = None

    def _mut(state: dict[str, Any]) -> None:
        nonlocal blocked_error
        batch = state.get(bid)
        if not isinstance(batch, dict):
            blocked_error = f"batch '{bid}' not found"
            return

        locked_until = int(batch.get("run_lock_until_ts") or 0)
        locked_by = str(batch.get("run_lock_owner") or "")
        if locked_until > now and locked_by and locked_by != owner:
            blocked_error = (
                f"batch is already running by another worker until {locked_until}; "
                "retry later or after lock lease expires"
            )
            return

        batch["run_lock_owner"] = owner
        batch["run_lock_until_ts"] = now + BATCH_RUN_LEASE_SEC
        state[bid] = batch

    update_json_dict(BATCH_FILE, _mut, root_key="batches")
    if blocked_error:
        return False, blocked_error
    return True, None


def _release_batch_run_lock(batch_id: str, now_ts: int | None = None) -> None:
    now = int(now_ts if now_ts is not None else time.time())
    owner = _batch_run_owner()
    bid = (batch_id or "").strip()

    def _mut(state: dict[str, Any]) -> None:
        batch = state.get(bid)
        if not isinstance(batch, dict):
            return
        if str(batch.get("run_lock_owner") or "") not in ("", owner):
            return
        batch["run_lock_owner"] = None
        batch["run_lock_until_ts"] = now
        state[bid] = batch

    update_json_dict(BATCH_FILE, _mut, root_key="batches")


def _summarize_batch(batch: dict[str, Any]) -> dict[str, Any]:
    return summarize_batch(batch)


def _create_add_member_batch_record(
    user: str,
    groups: list[str],
    note: str,
    ttl_hours: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    return create_add_member_batch_record(
        user=user,
        groups=groups,
        note=note,
        ttl_hours=ttl_hours,
        check_target_allowed=_check_target_allowed,
    )


def _create_delete_messages_batch_record(
    targets: list[dict[str, Any]],
    note: str,
    ttl_hours: int,
    max_ids_per_action: int,
    revoke: bool,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    return create_delete_messages_batch_record(
        targets=targets,
        note=note,
        ttl_hours=ttl_hours,
        max_ids_per_action=max_ids_per_action,
        revoke=revoke,
        check_target_allowed=_check_target_allowed,
    )


def _create_leave_dialog_batch_record(
    targets: list[str],
    note: str,
    ttl_hours: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    return create_leave_dialog_batch_record(
        targets=targets,
        note=note,
        ttl_hours=ttl_hours,
        check_target_allowed=_check_target_allowed,
    )


@mcp.tool()
async def tg_create_private_group(
    title: str,
    users: list[str] | None = None,
    about: str = "",
    kind: str = "basic",
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Create a private supergroup with confirmation and idempotency gates."""
    clean_title = str(title or "").strip()
    if not clean_title:
        return _blocked("title is empty")
    group_kind = str(kind or "basic").strip().lower()
    if group_kind not in {"basic", "supergroup"}:
        return _blocked("kind must be 'basic' or 'supergroup'")

    can_run, error = _check_action_preconditions(
        clean_title,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    normalized_users = [
        str(user).strip().lower() for user in (users or []) if str(user).strip()
    ]
    action_hash = _hash_payload(
        {
            "action": "create_private_group",
            "target": _normalize_target(clean_title),
            "users": normalized_users,
            "about": str(about or "").strip(),
            "kind": group_kind,
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if not dry_run and not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    try:
        result = await manager.create_private_group(
            clean_title,
            users=list(users or []),
            about=about,
            kind=group_kind,
            dry_run=dry_run,
        )
    except Exception as exc:
        return _blocked(str(exc))

    if not dry_run and result.get("success"):
        _mark_action_executed(action_hash)
    if dry_run and approval_meta:
        result.update(approval_meta)
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    return result


@mcp.tool()
async def tg_list_sessions() -> dict:
    """List available Telegram sessions in data/sessions/."""
    return await ctx.list_sessions()


@mcp.tool()
async def tg_use_session(session_name: str) -> dict:
    """Switch to a different Telegram session if allowed by configuration."""
    return await ctx.use_session(session_name)


@mcp.tool()
async def tg_get_group_info(group: str) -> dict:
    """Get group/channel info to validate the target before action calls."""
    manager = await ctx.get_manager()
    result = await manager.get_group_info(group)
    return result or {"error": "Group not found"}


@mcp.tool()
async def tg_resolve_username(username: str) -> dict:
    """Resolve a Telegram @username to user/channel/chat info."""
    manager = await ctx.get_manager()
    result = await manager.resolve_username(username)
    return result or {"error": f"Could not resolve username '{username}'"}


@mcp.tool()
async def tg_get_my_dialogs(limit: int = 100, dialog_type: str = "all") -> dict:
    """List dialogs to choose safe action targets."""
    manager = await ctx.get_manager()
    dialogs = await manager.get_my_dialogs(limit=limit, dialog_type=dialog_type)
    return {"count": len(dialogs), "dialogs": dialogs}


@mcp.tool()
async def tg_set_channel_comments_join_requirement(
    channel: str,
    join_required: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Toggle whether comments require joining the linked discussion group first."""
    manager = await ctx.get_manager()
    preview = await manager.set_channel_comments_join_requirement(
        channel_identifier=channel,
        join_required=join_required,
        dry_run=True,
    )
    if not preview.get("success"):
        return preview

    linked_group_target = str(preview.get("linked_group_target") or "").strip()
    can_run, error = _check_action_preconditions(
        linked_group_target,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(
            error or "preconditions failed",
            channel=channel,
            join_required=bool(join_required),
            linked_group_target=linked_group_target,
            linked_group_id=preview.get("linked_group_id"),
            linked_group_title=preview.get("linked_group_title"),
            current_join_required=preview.get("current_join_required"),
        )

    action_hash = _hash_payload(
        {
            "action": "set_channel_comments_join_requirement",
            "channel": _normalize_target(str(preview.get("channel_target") or channel)),
            "linked_group": _normalize_target(linked_group_target),
            "join_required": bool(join_required),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(
            approval_error or "approval gate blocked",
            channel=channel,
            join_required=bool(join_required),
            linked_group_target=linked_group_target,
        )

    if dry_run:
        result = dict(preview)
        result["action_hash"] = action_hash
        result["confirmation_text_required"] = (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        )
        if approval_meta:
            result.update(approval_meta)
        return result

    if not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    result = await manager.set_channel_comments_join_requirement(
        channel_identifier=channel,
        join_required=join_required,
        dry_run=False,
    )
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    if result.get("success"):
        _mark_action_executed(action_hash)
    return result


@mcp.tool()
async def tg_send_message(
    group: str,
    message_text: str,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Send message with policy gates (confirm + confirmation_text + idempotency)."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    clean_text = (message_text or "").strip()
    if not clean_text:
        return _blocked("message_text is empty")

    if len(clean_text) > MAX_MESSAGE_LEN:
        return {
            "success": False,
            "error": f"message_text is too long ({len(clean_text)} > {MAX_MESSAGE_LEN})",
        }

    action_hash = _hash_payload(
        {
            "action": "send_message",
            "target": _normalize_target(group),
            "text": clean_text,
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if dry_run:
        result = {
            "success": True,
            "dry_run": True,
            "target": group,
            "message_len": len(clean_text),
            "action_hash": action_hash,
            "confirmation_text_required": (
                CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
            ),
        }
        if approval_meta:
            result.update(approval_meta)
        return result

    if not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    sent = await manager.send_message(group, clean_text)
    if sent:
        _mark_action_executed(action_hash)
        return {
            "success": True,
            "target": group,
            "message_len": len(clean_text),
            "action_hash": action_hash,
        }

    return {
        "success": False,
        "target": group,
        "action_hash": action_hash,
        "error": "send_message failed (see server logs for details)",
    }


@mcp.tool()
async def tg_send_file(
    group: str,
    file_path: str,
    caption: str = "",
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Send local file with policy gates (confirm + confirmation_text + idempotency)."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    path = (file_path or "").strip()
    if not path:
        return _blocked("file_path is empty")
    if not os.path.exists(path):
        return _blocked(f"file_path does not exist: {path}")
    if not os.path.isfile(path):
        return _blocked(f"file_path is not a file: {path}")

    file_size_bytes = os.path.getsize(path)
    file_size_mb = file_size_bytes / (1024 * 1024)
    if file_size_mb > MAX_FILE_MB:
        return {
            "success": False,
            "error": f"file is too large ({file_size_mb:.2f} MB > {MAX_FILE_MB} MB)",
        }

    clean_caption = (caption or "").strip()
    if len(clean_caption) > MAX_MESSAGE_LEN:
        return {
            "success": False,
            "error": f"caption is too long ({len(clean_caption)} > {MAX_MESSAGE_LEN})",
        }

    stat = os.stat(path)
    action_hash = _hash_payload(
        {
            "action": "send_file",
            "target": _normalize_target(group),
            "file_path": os.path.abspath(path),
            "file_size": int(stat.st_size),
            "file_mtime_ns": int(stat.st_mtime_ns),
            "caption": clean_caption,
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if dry_run:
        result = {
            "success": True,
            "dry_run": True,
            "target": group,
            "file_path": path,
            "file_size_mb": round(file_size_mb, 3),
            "caption_len": len(clean_caption),
            "action_hash": action_hash,
            "confirmation_text_required": (
                CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
            ),
        }
        if approval_meta:
            result.update(approval_meta)
        return result

    if not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    sent = await manager.send_file(group, path, caption=clean_caption)
    if sent:
        _mark_action_executed(action_hash)
        return {
            "success": True,
            "target": group,
            "file_path": path,
            "file_size_mb": round(file_size_mb, 3),
            "caption_len": len(clean_caption),
            "action_hash": action_hash,
        }

    return {
        "success": False,
        "target": group,
        "action_hash": action_hash,
        "error": "send_file failed (see server logs for details)",
    }


@mcp.tool()
async def tg_delete_messages(
    group: str,
    message_ids: list[int],
    revoke: bool = True,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Delete specific messages in a dialog/group with policy gates."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    try:
        normalized_ids = _normalize_message_ids_arg(message_ids)
    except ValueError as exc:
        return _blocked(str(exc))

    action_hash = _hash_payload(
        {
            "action": "delete_messages",
            "target": _normalize_target(group),
            "message_ids": normalized_ids,
            "revoke": bool(revoke),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if dry_run:
        manager = await ctx.get_manager()
        result = await manager.delete_messages(
            group, normalized_ids, revoke=bool(revoke), dry_run=True
        )
        result["action_hash"] = action_hash
        result["confirmation_text_required"] = (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        )
        if approval_meta:
            result.update(approval_meta)
        return result

    if not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.delete_messages(
        group, normalized_ids, revoke=bool(revoke), dry_run=False
    )
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    if result.get("success"):
        _mark_action_executed(action_hash)
    return result


@mcp.tool()
async def tg_clear_history(
    group: str,
    max_id: int = 0,
    revoke: bool = True,
    just_clear: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Clear dialog history via DeleteHistoryRequest with policy gates."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    try:
        normalized_max_id = int(max_id or 0)
    except Exception:
        return _blocked(f"Invalid max_id: {max_id!r}")
    if normalized_max_id < 0:
        return _blocked("max_id must be >= 0")

    action_hash = _hash_payload(
        {
            "action": "clear_history",
            "target": _normalize_target(group),
            "max_id": normalized_max_id,
            "revoke": bool(revoke),
            "just_clear": bool(just_clear),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if dry_run:
        manager = await ctx.get_manager()
        result = await manager.clear_history(
            group,
            max_id=normalized_max_id,
            revoke=bool(revoke),
            just_clear=bool(just_clear),
            dry_run=True,
        )
        result["action_hash"] = action_hash
        result["confirmation_text_required"] = (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        )
        if approval_meta:
            result.update(approval_meta)
        return result

    if not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.clear_history(
        group,
        max_id=normalized_max_id,
        revoke=bool(revoke),
        just_clear=bool(just_clear),
        dry_run=False,
    )
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    if result.get("success"):
        _mark_action_executed(action_hash)
    return result


@mcp.tool()
async def tg_leave_dialog(
    group: str,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Leave a channel/group with policy gates. Direct user dialogs are not leaveable."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    action_hash = _hash_payload(
        {
            "action": "leave_dialog",
            "target": _normalize_target(group),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if not dry_run and not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.leave_dialog(group, dry_run=dry_run)
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    if dry_run and approval_meta:
        result.update(approval_meta)
    if not dry_run and result.get("success"):
        _mark_action_executed(action_hash)
    return result


@mcp.tool()
async def tg_edit_message(
    group: str,
    message_id: int,
    new_text: str,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Edit a message in a dialog/group with policy gates."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    try:
        normalized_message_id = int(message_id)
    except Exception:
        return _blocked(f"Invalid message_id: {message_id!r}")
    if normalized_message_id <= 0:
        return _blocked("message_id must be > 0")

    clean_text = (new_text or "").strip()
    if not clean_text:
        return _blocked("new_text is empty")
    if len(clean_text) > MAX_MESSAGE_LEN:
        return {
            "success": False,
            "error": f"new_text is too long ({len(clean_text)} > {MAX_MESSAGE_LEN})",
        }

    action_hash = _hash_payload(
        {
            "action": "edit_message",
            "target": _normalize_target(group),
            "message_id": normalized_message_id,
            "new_text": clean_text,
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if dry_run:
        manager = await ctx.get_manager()
        result = await manager.edit_message(
            group,
            message_id=normalized_message_id,
            new_text=clean_text,
            dry_run=True,
        )
        result["action_hash"] = action_hash
        result["confirmation_text_required"] = (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        )
        if approval_meta:
            result.update(approval_meta)
        return result

    if not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.edit_message(
        group,
        message_id=normalized_message_id,
        new_text=clean_text,
        dry_run=False,
    )
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    if result.get("success"):
        _mark_action_executed(action_hash)
    return result


@mcp.tool()
async def tg_forward_messages(
    from_group: str,
    to_group: str,
    message_ids: list[int],
    drop_author: bool = False,
    drop_media_captions: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Forward messages between dialogs with policy gates."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    allowed_source, source_error = _check_target_allowed(from_group)
    if not allowed_source:
        return _blocked(source_error or "source target is not allowed")

    can_run, error = _check_action_preconditions(
        to_group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    try:
        normalized_ids = _normalize_message_ids_arg(message_ids)
    except ValueError as exc:
        return _blocked(str(exc))

    action_hash = _hash_payload(
        {
            "action": "forward_messages",
            "from_target": _normalize_target(from_group),
            "to_target": _normalize_target(to_group),
            "message_ids": normalized_ids,
            "drop_author": bool(drop_author),
            "drop_media_captions": bool(drop_media_captions),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if dry_run:
        manager = await ctx.get_manager()
        result = await manager.forward_messages(
            from_group,
            to_group,
            normalized_ids,
            drop_author=bool(drop_author),
            drop_media_captions=bool(drop_media_captions),
            dry_run=True,
        )
        result["action_hash"] = action_hash
        result["confirmation_text_required"] = (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        )
        if approval_meta:
            result.update(approval_meta)
        return result

    if not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.forward_messages(
        from_group,
        to_group,
        normalized_ids,
        drop_author=bool(drop_author),
        drop_media_captions=bool(drop_media_captions),
        dry_run=False,
    )
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    if result.get("success"):
        _mark_action_executed(action_hash)
    return result


@mcp.tool()
async def tg_add_member_to_group(
    group: str,
    user: str,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Add user to group/channel with confirmation and idempotency gates."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    action_hash = _hash_payload(
        {
            "action": "add_member",
            "target": _normalize_target(group),
            "user": str(user).strip().lower(),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if not dry_run and not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.add_member_to_group(group, user, dry_run=dry_run)
    if not dry_run and result.get("success"):
        _mark_action_executed(action_hash)
    if dry_run and approval_meta:
        result.update(approval_meta)
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    return result


@mcp.tool()
async def tg_remove_member_from_group(
    group: str,
    user: str,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Remove user from group/channel with confirmation and idempotency gates."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    action_hash = _hash_payload(
        {
            "action": "remove_member",
            "target": _normalize_target(group),
            "user": str(user).strip().lower(),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if not dry_run and not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.remove_member_from_group(group, user, dry_run=dry_run)
    if not dry_run and result.get("success"):
        _mark_action_executed(action_hash)
    if dry_run and approval_meta:
        result.update(approval_meta)
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    return result


@mcp.tool()
async def tg_migrate_member(
    group: str,
    old_user: str,
    new_user: str,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Migrate member (add new, remove old) with confirmation and idempotency gates."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    action_hash = _hash_payload(
        {
            "action": "migrate_member",
            "target": _normalize_target(group),
            "old_user": str(old_user).strip().lower(),
            "new_user": str(new_user).strip().lower(),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    if not dry_run and not force_resend:
        duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
        if duplicate:
            return {
                "success": False,
                "duplicate_blocked": True,
                "retry_after_sec": retry_after_sec,
                "action_hash": action_hash,
                "error": "Duplicate action blocked by idempotency window. "
                "Set force_resend=true to override.",
            }

    manager = await ctx.get_manager()
    result = await manager.migrate_member(
        group_identifier=group,
        old_user_identifier=old_user,
        new_user_identifier=new_user,
        dry_run=dry_run,
    )
    if not dry_run and result.get("success"):
        _mark_action_executed(action_hash)
    if dry_run and approval_meta:
        result.update(approval_meta)
    result["action_hash"] = action_hash
    result["confirmation_text_required"] = (
        CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
    )
    return result


def _load_json_file(path_arg: str) -> Any:
    path = Path((path_arg or "").strip()).expanduser()
    if not path.exists():
        raise ValueError(f"path does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"path is not a file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_targets(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("targets"), list):
        return [item for item in payload["targets"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _leave_targets_from_candidates(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        raw_items = payload.get("candidates") or payload.get("targets") or []
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    targets: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            targets.append(item)
            continue
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        approved = bool(item.get("leave_candidate") or review.get("leave_candidate"))
        if not approved:
            continue
        target = item.get("target") or item.get("group") or item.get("chat_id")
        if target is not None:
            targets.append(str(target))
    return targets


@mcp.tool()
async def tg_create_add_member_batch(
    user: str,
    groups: list[str],
    note: str = "",
    ttl_hours: int = BATCH_DEFAULT_TTL_HOURS,
) -> dict:
    """Create batch for adding one user to many groups with one-time approval."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    if not str(user).strip():
        return _blocked("user is empty")
    if not groups:
        return _blocked("groups list is empty")

    batch, blocked_targets = _create_add_member_batch_record(
        user=user, groups=groups, note=note, ttl_hours=ttl_hours
    )
    state = _load_batches_state()
    state[batch["id"]] = batch
    _save_batches_state(state)

    summary = _summarize_batch(batch)
    summary["blocked_targets"] = blocked_targets
    summary["next_step"] = (
        "Call tg_approve_batch(batch_id, confirmation_text), "
        "then tg_run_add_member_batch(batch_id)."
    )
    return {"success": True, **summary}


@mcp.tool()
async def tg_create_delete_messages_batch_from_manifest(
    manifest_path: str,
    note: str = "",
    max_ids_per_action: int = 100,
    revoke: bool = True,
    ttl_hours: int = BATCH_DEFAULT_TTL_HOURS,
) -> dict:
    """Create approved-later delete batch from privacy scrubber delete_manifest.json."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    try:
        payload = _load_json_file(manifest_path)
    except Exception as exc:
        return _blocked(f"failed to load manifest: {exc}")
    targets = _manifest_targets(payload)
    if not targets:
        return _blocked("manifest has no valid targets")

    batch, blocked_targets = _create_delete_messages_batch_record(
        targets=targets,
        note=note or f"from_manifest:{Path(manifest_path).name}",
        ttl_hours=ttl_hours,
        max_ids_per_action=max_ids_per_action,
        revoke=revoke,
    )
    if not batch.get("actions"):
        return _blocked("manifest produced no delete actions")
    state = _load_batches_state()
    state[batch["id"]] = batch
    _save_batches_state(state)

    summary = _summarize_batch(batch)
    summary["blocked_targets"] = blocked_targets
    summary["next_step"] = (
        "Call tg_approve_batch(batch_id, confirmation_text), "
        "then tg_run_delete_messages_batch(batch_id)."
    )
    return {"success": True, **summary}


@mcp.tool()
async def tg_create_leave_dialog_batch_from_candidates(
    candidates_path: str,
    note: str = "",
    ttl_hours: int = BATCH_DEFAULT_TTL_HOURS,
) -> dict:
    """Create approved-later leave-dialog batch from reviewed channel_candidates.json."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    try:
        payload = _load_json_file(candidates_path)
    except Exception as exc:
        return _blocked(f"failed to load candidates: {exc}")
    targets = _leave_targets_from_candidates(payload)
    if not targets:
        return _blocked("candidates file has no leave_candidate=true targets")

    batch, blocked_targets = _create_leave_dialog_batch_record(
        targets=targets,
        note=note or f"from_candidates:{Path(candidates_path).name}",
        ttl_hours=ttl_hours,
    )
    state = _load_batches_state()
    state[batch["id"]] = batch
    _save_batches_state(state)

    summary = _summarize_batch(batch)
    summary["blocked_targets"] = blocked_targets
    summary["next_step"] = (
        "Call tg_approve_batch(batch_id, confirmation_text), "
        "then tg_run_leave_dialog_batch(batch_id)."
    )
    return {"success": True, **summary}


@mcp.tool()
async def tg_create_add_member_batch_from_report(
    report_path: str,
    user: str,
    note: str = "",
    error_contains: str = "join quota exceeded",
    ttl_hours: int = BATCH_DEFAULT_TTL_HOURS,
) -> dict:
    """Create add-member batch from JSON report (e.g. previous migration run)."""
    path = Path((report_path or "").strip())
    if not path.exists():
        return _blocked(f"report_path does not exist: {path}")
    if not path.is_file():
        return _blocked(f"report_path is not a file: {path}")

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _blocked(f"failed to parse report: {exc}")

    items = report.get("items")
    if not isinstance(items, list):
        return _blocked("report has no valid 'items' array")

    needle = (error_contains or "").strip().lower()
    groups: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("success"):
            continue
        err = str(result.get("error", "")).lower()
        if needle and needle not in err:
            continue
        chat_id = item.get("chat_id")
        if chat_id is None:
            continue
        groups.append(str(chat_id))

    if not groups:
        return {
            "success": False,
            "error": f"No failed groups matched error_contains='{error_contains}' in report.",
        }

    note_prefix = f"from_report:{path.name}"
    full_note = f"{note_prefix} {note}".strip()
    return await tg_create_add_member_batch(
        user=user,
        groups=groups,
        note=full_note,
        ttl_hours=ttl_hours,
    )


@mcp.tool()
async def tg_approve_batch(batch_id: str, confirmation_text: str) -> dict:
    """Approve previously created batch once; after that runs don't need per-action approval."""
    state, batch = _get_batch(batch_id)
    if not batch:
        return _blocked(f"batch '{batch_id}' not found")

    now = int(time.time())
    if int(batch.get("expires_at_ts", 0)) <= now:
        return _blocked("batch is expired")

    ok, err = _validate_confirmation_text(confirmation_text, dry_run=False)
    if not ok:
        return _blocked(err or "confirmation_text validation failed")

    batch["approved"] = True
    batch["approved_at_ts"] = now
    batch["approved_until_ts"] = now + BATCH_APPROVAL_LEASE_SEC
    if batch.get("status") == "pending_approval":
        batch["status"] = "approved"
    state[batch["id"]] = batch
    _save_batches_state(state)

    result = {"success": True, **_summarize_batch(batch)}
    result["approval_lease_sec"] = BATCH_APPROVAL_LEASE_SEC
    return result


@mcp.tool()
async def tg_get_batch_status(batch_id: str) -> dict:
    """Get status and counters for action batch."""
    _, batch = _get_batch(batch_id)
    if not batch:
        return _blocked(f"batch '{batch_id}' not found")

    summary = _summarize_batch(batch)
    pending_groups = [
        action.get("group")
        for action in batch.get("actions", [])
        if action.get("status") == "pending"
    ]
    summary["pending_groups_preview"] = pending_groups[:20]
    summary["last_error"] = batch.get("last_error")
    return {"success": True, **summary}


def _batch_preflight(
    batch_id: str, max_actions: int, expected_type: str
) -> tuple[
    dict[str, dict[str, Any]] | None, dict[str, Any] | None, dict[str, Any] | None
]:
    if SAFE_STARTUP_BLOCK_REASON:
        return None, None, _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return None, None, _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")
    if max_actions <= 0:
        return None, None, _blocked("max_actions must be > 0")

    state, batch = _get_batch(batch_id)
    if not batch:
        return None, None, _blocked(f"batch '{batch_id}' not found")
    if batch.get("type") != expected_type:
        return (
            None,
            None,
            _blocked(
                f"batch '{batch_id}' has type {batch.get('type')!r}, expected {expected_type!r}",
                **_summarize_batch(batch),
            ),
        )
    return state, batch, None


def _mark_batch_not_runnable(
    state: dict[str, dict[str, Any]],
    batch: dict[str, Any],
    *,
    status: str,
    error: str,
    now: int,
) -> dict:
    batch["status"] = status
    if status == "pending_approval":
        batch["approved"] = False
    batch["last_error"] = error
    batch["run_lock_owner"] = None
    batch["run_lock_until_ts"] = now
    state[batch["id"]] = batch
    _save_batches_state(state)
    return _blocked(error, **_summarize_batch(batch))


def _finalize_batch_run(
    state: dict[str, dict[str, Any]],
    batch: dict[str, Any],
    *,
    now: int,
    processed_now: int,
    stopped_reason: str | None,
) -> dict:
    pending_left = any(a.get("status") == "pending" for a in batch.get("actions", []))
    if batch.get("status") == "running":
        batch["status"] = "approved" if pending_left else "completed"
    if batch.get("status") == "completed":
        batch["completed_at_ts"] = now
    batch["last_run_ts"] = now
    batch["run_lock_owner"] = None
    batch["run_lock_until_ts"] = now
    state[batch["id"]] = batch
    _save_batches_state(state)
    summary = _summarize_batch(batch)
    summary["processed_now"] = processed_now
    summary["stopped_reason"] = stopped_reason
    summary["last_error"] = batch.get("last_error")
    return {"success": True, **summary}


def _prepare_batch_for_run(
    batch_id: str, max_actions: int, expected_type: str
) -> tuple[
    dict[str, dict[str, Any]] | None,
    dict[str, Any] | None,
    int | None,
    dict[str, Any] | None,
]:
    state, batch, blocked = _batch_preflight(batch_id, max_actions, expected_type)
    if blocked:
        return None, None, None, blocked
    assert batch is not None
    now = int(time.time())
    lock_ok, lock_error = _acquire_batch_run_lock(batch_id, now_ts=now)
    if not lock_ok:
        return (
            None,
            None,
            None,
            _blocked(
                lock_error or "failed to acquire batch run lock",
                **_summarize_batch(batch),
            ),
        )

    state, batch = _get_batch(batch_id)
    if not batch:
        return None, None, None, _blocked(f"batch '{batch_id}' not found")

    if int(batch.get("expires_at_ts", 0)) <= now:
        return (
            state,
            batch,
            now,
            _mark_batch_not_runnable(
                state, batch, status="expired", error="batch is expired", now=now
            ),
        )
    if not bool(batch.get("approved", False)):
        return (
            state,
            batch,
            now,
            _mark_batch_not_runnable(
                state,
                batch,
                status="pending_approval",
                error="batch is not approved; call tg_approve_batch first",
                now=now,
            ),
        )
    approved_until_ts = int(batch.get("approved_until_ts") or 0)
    if approved_until_ts <= now:
        return (
            state,
            batch,
            now,
            _mark_batch_not_runnable(
                state,
                batch,
                status="pending_approval",
                error="batch approval expired; call tg_approve_batch again",
                now=now,
            ),
        )
    if batch.get("status") == "completed":
        batch["run_lock_owner"] = None
        batch["run_lock_until_ts"] = now
        state[batch["id"]] = batch
        _save_batches_state(state)
        return (
            state,
            batch,
            now,
            {
                "success": True,
                "message": "batch already completed",
                **_summarize_batch(batch),
            },
        )
    return state, batch, now, None


@mcp.tool()
async def tg_run_delete_messages_batch(batch_id: str, max_actions: int = 25) -> dict:
    """Execute approved delete-messages batch without per-action confirmations."""
    state, batch, now, blocked = _prepare_batch_for_run(
        batch_id, max_actions, "delete_messages"
    )
    if blocked:
        return blocked
    assert state is not None and batch is not None and now is not None

    manager = await ctx.get_manager()
    processed_now = 0
    stopped_reason = None
    batch["status"] = "running"
    batch["last_error"] = None

    for action in batch.get("actions", []):
        if processed_now >= int(max_actions):
            break
        if action.get("status") != "pending":
            continue
        group = str(action.get("group"))
        allowed, allowed_error = _check_target_allowed(group)
        if not allowed:
            action["status"] = "blocked_policy"
            action["last_error"] = allowed_error
            action["last_run_ts"] = now
            processed_now += 1
            continue

        result = await manager.delete_messages(
            group,
            action.get("message_ids") or [],
            revoke=bool(batch.get("revoke", True)),
            dry_run=False,
        )
        action["attempts"] = int(action.get("attempts", 0)) + 1
        action["last_run_ts"] = now
        if result.get("success"):
            action["status"] = "success"
            action["last_error"] = None
            _mark_action_executed(str(action.get("action_hash", "")))
        else:
            err_text = str(result.get("error", "unknown error"))
            action["status"] = "failed"
            action["last_error"] = err_text
            batch["last_error"] = err_text
        processed_now += 1

    return _finalize_batch_run(
        state,
        batch,
        now=now,
        processed_now=processed_now,
        stopped_reason=stopped_reason,
    )


@mcp.tool()
async def tg_run_leave_dialog_batch(batch_id: str, max_actions: int = 20) -> dict:
    """Execute approved leave-dialog batch without per-action confirmations."""
    state, batch, now, blocked = _prepare_batch_for_run(
        batch_id, max_actions, "leave_dialog"
    )
    if blocked:
        return blocked
    assert state is not None and batch is not None and now is not None

    manager = await ctx.get_manager()
    processed_now = 0
    stopped_reason = None
    batch["status"] = "running"
    batch["last_error"] = None

    for action in batch.get("actions", []):
        if processed_now >= int(max_actions):
            break
        if action.get("status") != "pending":
            continue
        group = str(action.get("group"))
        allowed, allowed_error = _check_target_allowed(group)
        if not allowed:
            action["status"] = "blocked_policy"
            action["last_error"] = allowed_error
            action["last_run_ts"] = now
            processed_now += 1
            continue

        result = await manager.leave_dialog(group, dry_run=False)
        action["attempts"] = int(action.get("attempts", 0)) + 1
        action["last_run_ts"] = now
        if result.get("success"):
            action["status"] = "success"
            action["last_error"] = None
            _mark_action_executed(str(action.get("action_hash", "")))
        else:
            err_text = str(result.get("error", "unknown error"))
            action["status"] = "failed"
            action["last_error"] = err_text
            batch["last_error"] = err_text
        processed_now += 1

    return _finalize_batch_run(
        state,
        batch,
        now=now,
        processed_now=processed_now,
        stopped_reason=stopped_reason,
    )


@mcp.tool()
async def tg_run_add_member_batch(batch_id: str, max_actions: int = 100) -> dict:
    """Execute approved add-member batch without per-action confirmations."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    if max_actions <= 0:
        return _blocked("max_actions must be > 0")

    state, batch = _get_batch(batch_id)
    if not batch:
        return _blocked(f"batch '{batch_id}' not found")

    now = int(time.time())
    lock_ok, lock_error = _acquire_batch_run_lock(batch_id, now_ts=now)
    if not lock_ok:
        return _blocked(
            lock_error or "failed to acquire batch run lock", **_summarize_batch(batch)
        )

    try:
        state, batch = _get_batch(batch_id)
        if not batch:
            return _blocked(f"batch '{batch_id}' not found")

        if int(batch.get("expires_at_ts", 0)) <= now:
            batch["status"] = "expired"
            batch["run_lock_owner"] = None
            batch["run_lock_until_ts"] = now
            state[batch["id"]] = batch
            _save_batches_state(state)
            return _blocked("batch is expired", **_summarize_batch(batch))

        if not bool(batch.get("approved", False)):
            batch["run_lock_owner"] = None
            batch["run_lock_until_ts"] = now
            state[batch["id"]] = batch
            _save_batches_state(state)
            return _blocked(
                "batch is not approved; call tg_approve_batch first",
                **_summarize_batch(batch),
            )

        approved_until_ts = int(batch.get("approved_until_ts") or 0)
        if approved_until_ts <= now:
            batch["approved"] = False
            batch["status"] = "pending_approval"
            batch["run_lock_owner"] = None
            batch["run_lock_until_ts"] = now
            state[batch["id"]] = batch
            _save_batches_state(state)
            return _blocked(
                "batch approval expired; call tg_approve_batch again",
                **_summarize_batch(batch),
            )

        if batch.get("status") == "completed":
            batch["run_lock_owner"] = None
            batch["run_lock_until_ts"] = now
            state[batch["id"]] = batch
            _save_batches_state(state)
            return {
                "success": True,
                "message": "batch already completed",
                **_summarize_batch(batch),
            }

        manager = await ctx.get_manager()
        processed_now = 0
        stopped_reason = None

        batch["status"] = "running"
        batch["last_error"] = None

        for action in batch.get("actions", []):
            if processed_now >= int(max_actions):
                break
            if action.get("status") != "pending":
                continue

            group = str(action.get("group"))
            allowed, allowed_error = _check_target_allowed(group)
            if not allowed:
                action["status"] = "blocked_policy"
                action["last_error"] = allowed_error
                action["last_run_ts"] = now
                processed_now += 1
                continue

            result = await manager.add_member_to_group(
                group, batch.get("user"), dry_run=False
            )
            action["attempts"] = int(action.get("attempts", 0)) + 1
            action["last_run_ts"] = now

            if result.get("success"):
                if result.get("already_member"):
                    action["status"] = "already_member"
                else:
                    action["status"] = "success"
                    _mark_action_executed(str(action.get("action_hash", "")))
                action["last_error"] = None
                processed_now += 1
                continue

            err_text = str(result.get("error", "unknown error"))
            err_lower = err_text.lower()
            action["last_error"] = err_text

            if "join quota exceeded" in err_lower:
                batch["status"] = "paused_quota"
                batch["last_error"] = err_text
                stopped_reason = "join_quota_exceeded"
                break

            if "you can't write in this chat" in err_lower:
                action["status"] = "blocked_rights"
            else:
                action["status"] = "failed"
            processed_now += 1

        pending_left = any(
            a.get("status") == "pending" for a in batch.get("actions", [])
        )
        if batch.get("status") == "running":
            batch["status"] = "approved" if pending_left else "completed"
        if batch.get("status") == "completed":
            batch["completed_at_ts"] = now
        batch["last_run_ts"] = now
        batch["run_lock_owner"] = None
        batch["run_lock_until_ts"] = now

        state[batch["id"]] = batch
        _save_batches_state(state)

        summary = _summarize_batch(batch)
        summary["processed_now"] = processed_now
        summary["stopped_reason"] = stopped_reason
        return {"success": True, **summary}
    finally:
        _release_batch_run_lock(batch_id, now_ts=int(time.time()))


@mcp.tool()
async def tg_get_actions_policy() -> dict[str, Any]:
    """Return active action policy gates and limits."""
    limiter_stats = get_rate_limiter().get_stats()
    session_path_status = ctx.session_path_status()
    return {
        "server_profile": "actions",
        "actions_enabled": ACTIONS_ENABLED,
        "require_allowlist": REQUIRE_ALLOWLIST,
        "allowed_targets": sorted(ALLOWED_TARGETS),
        "max_message_len": MAX_MESSAGE_LEN,
        "max_file_mb": MAX_FILE_MB,
        "idempotency_enabled": IDEMPOTENCY_ENABLED,
        "idempotency_window_sec": IDEMPOTENCY_WINDOW_SEC,
        "require_confirmation_text": REQUIRE_CONFIRMATION_TEXT,
        "confirmation_phrase": (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        ),
        "min_confirmation_text_len": MIN_CONFIRMATION_TEXT_LEN,
        "require_approval_code": REQUIRE_APPROVAL_CODE,
        "approval_ttl_sec": APPROVAL_TTL_SEC if REQUIRE_APPROVAL_CODE else None,
        "approval_min_age_sec": APPROVAL_MIN_AGE_SEC if REQUIRE_APPROVAL_CODE else None,
        "batch_file": str(BATCH_FILE),
        "batch_default_ttl_hours": BATCH_DEFAULT_TTL_HOURS,
        "batch_approval_lease_sec": BATCH_APPROVAL_LEASE_SEC,
        "batch_run_lease_sec": BATCH_RUN_LEASE_SEC,
        "unsafe_override": UNSAFE_OVERRIDE,
        "unsafe_policy_issues": UNSAFE_POLICY_ISSUES,
        "safe_startup_block_reason": SAFE_STARTUP_BLOCK_REASON,
        "write_context": os.environ.get("TG_WRITE_CONTEXT"),
        "direct_telethon_write_guard": os.environ.get(
            "TG_BLOCK_DIRECT_TELETHON_WRITE", "1"
        )
        == "1",
        "enforce_action_process": os.environ.get("TG_ENFORCE_ACTION_PROCESS", "1")
        == "1",
        "group_msg_usage": limiter_stats.get("group_msg_usage"),
        "circuit_breaker": limiter_stats.get("circuit_breaker"),
        "destructive_actions_require_confirm": True,
        "default_dry_run_for_member_actions": True,
        "allow_session_switch": ALLOW_SESSION_SWITCH,
        "session_path_status": session_path_status,
        "session_path_conflict": session_path_status.get("conflict"),
        "recommended_write_flow": [
            "1) Call write tool with dry_run=true to preview and get approval_code.",
            "2) Ask user for exact confirmation_text phrase in this thread.",
            "3) Execute same payload with confirm=true + confirmation_text + approval_code.",
            "4) Handle duplicate_blocked by waiting or using force_resend=true intentionally.",
        ],
        "recommended_batch_flow": [
            "1) Create a batch: tg_create_add_member_batch, "
            "tg_create_delete_messages_batch_from_manifest, "
            "or tg_create_leave_dialog_batch_from_candidates.",
            "2) tg_approve_batch(batch_id, confirmation_text).",
            "3) Repeat the matching run tool until completed.",
            "4) If lease expires, re-run tg_approve_batch and continue.",
        ],
    }


@mcp.tool()
async def tg_get_stats() -> dict:
    """Get anti-spam statistics (API calls, flood waits, quotas, latency histogram)."""
    limiter = get_rate_limiter()
    return {
        "rate_limiter": limiter.get_stats(),
        "metrics": snapshot(),
        "current_session": ctx.current_session,
    }


@mcp.tool()
async def tg_auth_status() -> dict:
    """Check whether current/default Telegram session is authorized."""
    return await ctx.auth_status()


if __name__ == "__main__":
    mcp.run(transport="stdio")
