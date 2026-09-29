"""Actions-focused MCP server for tganalytics.

Contains high-risk Telegram operations behind explicit env gates.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.environ.get("TG_ENV_FILE") or None)

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
from mcp_actions_lane import (  # noqa: E402
    create_write_lane_record,
    lane_active_hours_error,
    lane_scope_hash,
    summarize_write_lane,
    validate_lane_message,
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
from tganalytics.domain.bot_workflow import (  # noqa: E402
    button_data_bytes as _button_data_bytes,
    message_button_options as _message_button_options,
    normalize_bot_steps,
    public_bot_steps as _public_bot_steps,
    public_button_options as _public_button_options,
    select_button_option as _select_button_option,
)
from tganalytics.infra.paths import resolve_state_path  # noqa: E402

from tganalytics.infra.limiter import get_rate_limiter, safe_call  # noqa: E402
from tganalytics.infra.managed_bot import (  # noqa: E402
    CheckManagedBotUsernameRequest,
    CreateManagedBotRequest,
)
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
    MAX_BOT_STEPS = int(os.environ.get("TG_ACTIONS_MAX_BOT_STEPS", "30"))
except ValueError:
    MAX_BOT_STEPS = 30

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
CONFIRMATION_PHRASE = (
    os.environ.get("TG_ACTIONS_CONFIRMATION_PHRASE", "").strip().lower()
)
REQUIRE_APPROVAL_CODE = os.environ.get("TG_ACTIONS_REQUIRE_APPROVAL_CODE", "1") == "1"
IDEMPOTENCY_ENABLED = os.environ.get("TG_ACTIONS_IDEMPOTENCY_ENABLED", "1") == "1"
IDEMPOTENCY_FILE = resolve_state_path(
    os.environ.get("TG_ACTIONS_IDEMPOTENCY_FILE", ""),
    "anti_spam",
    "action_idempotency.json",
)

try:
    APPROVAL_TTL_SEC = int(os.environ.get("TG_ACTIONS_APPROVAL_TTL_SEC", "1800"))
except ValueError:
    APPROVAL_TTL_SEC = 1800

try:
    APPROVAL_MIN_AGE_SEC = int(os.environ.get("TG_ACTIONS_APPROVAL_MIN_AGE_SEC", "30"))
except ValueError:
    APPROVAL_MIN_AGE_SEC = 30

APPROVAL_FILE = resolve_state_path(
    os.environ.get("TG_ACTIONS_APPROVAL_FILE", ""),
    "anti_spam",
    "action_approvals.json",
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

BATCH_FILE = resolve_state_path(
    os.environ.get("TG_ACTIONS_BATCH_FILE", ""),
    "anti_spam",
    "action_batches.json",
)

LANE_FILE = resolve_state_path(
    os.environ.get("TG_ACTIONS_LANE_FILE", ""), "anti_spam", "action_lanes.json"
)

try:
    LANE_MAX_TTL_SEC = int(
        os.environ.get("TG_ACTIONS_LANE_MAX_TTL_SEC", str(24 * 3600))
    )
except ValueError:
    LANE_MAX_TTL_SEC = 24 * 3600

try:
    LANE_APPROVAL_TTL_SEC = int(
        os.environ.get("TG_ACTIONS_LANE_APPROVAL_TTL_SEC", str(APPROVAL_TTL_SEC))
    )
except ValueError:
    LANE_APPROVAL_TTL_SEC = APPROVAL_TTL_SEC

try:
    LANE_MAX_TARGETS = int(os.environ.get("TG_ACTIONS_LANE_MAX_TARGETS", "20"))
except ValueError:
    LANE_MAX_TARGETS = 20

try:
    LANE_MAX_MESSAGES = int(os.environ.get("TG_ACTIONS_LANE_MAX_MESSAGES", "50"))
except ValueError:
    LANE_MAX_MESSAGES = 50

try:
    LANE_MIN_INTERVAL_SEC = int(
        os.environ.get("TG_ACTIONS_LANE_MIN_INTERVAL_SEC", "30")
    )
except ValueError:
    LANE_MIN_INTERVAL_SEC = 30

try:
    LANE_SEND_LOCK_SEC = int(os.environ.get("TG_ACTIONS_LANE_SEND_LOCK_SEC", "120"))
except ValueError:
    LANE_SEND_LOCK_SEC = 120

try:
    LANE_AUDIT_MAX_RECORDS = int(
        os.environ.get("TG_ACTIONS_LANE_AUDIT_MAX_RECORDS", "200")
    )
except ValueError:
    LANE_AUDIT_MAX_RECORDS = 200


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


def _normalize_bot_steps(steps: Any) -> tuple[list[dict[str, Any]], str | None]:
    return normalize_bot_steps(
        steps, max_steps=MAX_BOT_STEPS, max_message_len=MAX_MESSAGE_LEN
    )


def _is_callback_timeout_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "callback query in time" in text
        or "bot did not answer" in text
        or isinstance(exc, asyncio.TimeoutError)
    )


async def _find_button_for_step(
    entity: Any, step: dict[str, Any]
) -> tuple[Any, dict, list]:
    message_id = int(step.get("message_id") or 0)
    messages: list[Any]
    if message_id > 0:
        msg = await safe_call(
            ctx.client.get_messages,
            entity,
            ids=message_id,
            operation_type="api",
        )
        messages = [msg] if msg else []
    else:
        recent_limit = int(step.get("recent_limit") or 12)
        fetched = await safe_call(
            ctx.client.get_messages,
            entity,
            limit=recent_limit,
            operation_type="api",
        )
        messages = list(fetched or [])

    inspected: list[dict[str, Any]] = []
    for msg in messages:
        options = _message_button_options(msg)
        public_options = _public_button_options(options)
        inspected.append(
            {
                "message_id": getattr(msg, "id", None),
                "text": str(getattr(msg, "message", "") or "")[:300],
                "buttons": public_options,
            }
        )
        selected, error = _select_button_option(
            options,
            button_text=str(step.get("button_text") or ""),
            row=int(step.get("row", -1)),
            col=int(step.get("col", -1)),
            button_data_b64=str(step.get("button_data_b64") or ""),
            exact_text=bool(step.get("exact_text", True)),
        )
        if selected and not error:
            return msg, selected, inspected

    raise ValueError("button selector did not match recent messages")


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


def _load_lanes_state() -> dict[str, dict[str, Any]]:
    lanes = load_json_dict(LANE_FILE, root_key="lanes")
    return {str(k): v for k, v in lanes.items() if isinstance(v, dict)}


def _save_lanes_state(state: dict[str, dict[str, Any]]) -> None:
    normalized = {str(k): v for k, v in state.items() if isinstance(v, dict)}

    def _mut(current: dict[str, Any]) -> None:
        current.clear()
        current.update(normalized)

    update_json_dict(LANE_FILE, _mut, root_key="lanes")


def _append_lane_audit(lane: dict[str, Any], event: dict[str, Any]) -> None:
    audit = list(lane.get("audit") or [])
    audit.append(event)
    max_records = max(10, int(LANE_AUDIT_MAX_RECORDS))
    lane["audit"] = audit[-max_records:]


def _refresh_lane_status(lane: dict[str, Any], now_ts: int) -> None:
    status = str(lane.get("status") or "")
    if status in {"pending_approval", "active"} and str(
        lane.get("scope_hash") or ""
    ) != lane_scope_hash(lane):
        lane["approved"] = False
        lane["status"] = "invalid_scope"
        lane["last_error"] = "write lane scope integrity check failed"
        return
    if (
        status == "pending_approval"
        and int(lane.get("approval_deadline_ts") or 0) <= now_ts
    ):
        lane["status"] = "expired"
        lane["last_error"] = "lane approval window expired"
        return
    if status == "active" and int(lane.get("expires_at_ts") or 0) <= now_ts:
        lane["status"] = "expired"
        lane["last_error"] = "lane lease expired"
        return
    if status == "active" and int(lane.get("sent_count") or 0) >= int(
        lane.get("max_messages") or 0
    ):
        lane["status"] = "exhausted"
        lane["last_error"] = "lane message quota exhausted"


def _get_write_lane(lane_id: str) -> tuple[dict[str, Any] | None, str | None]:
    lane_key = str(lane_id or "").strip()
    if not lane_key:
        return None, "lane_id is empty"
    now = int(time.time())

    def _mut(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        lane = state.get(lane_key)
        if not isinstance(lane, dict):
            return None, f"write lane '{lane_key}' not found"
        _refresh_lane_status(lane, now)
        state[lane_key] = lane
        return dict(lane), None

    return update_json_dict(LANE_FILE, _mut, root_key="lanes")


def _record_lane_attempt(
    lane_id: str,
    *,
    target: str,
    outcome: str,
    action_hash: str | None = None,
    message_len: int | None = None,
    error: str | None = None,
    now_ts: int | None = None,
) -> None:
    lane_key = str(lane_id or "").strip()
    if not lane_key:
        return
    now = int(now_ts if now_ts is not None else time.time())

    def _mut(state: dict[str, Any]) -> None:
        lane = state.get(lane_key)
        if not isinstance(lane, dict):
            return
        _refresh_lane_status(lane, now)
        event: dict[str, Any] = {
            "event_id": f"evt_{secrets.token_urlsafe(6)}",
            "ts": now,
            "target": _normalize_target(target),
            "outcome": str(outcome),
        }
        if action_hash:
            event["action_hash"] = action_hash
        if message_len is not None:
            event["message_len"] = int(message_len)
        if error:
            event["error"] = str(error)[:300]
            lane["last_error"] = str(error)[:300]
        _append_lane_audit(lane, event)
        state[lane_key] = lane

    update_json_dict(LANE_FILE, _mut, root_key="lanes")


def _acquire_lane_send(
    lane_id: str,
    *,
    target: str,
    now_ts: int | None = None,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    lane_key = str(lane_id or "").strip()
    normalized_target = _normalize_target(target)
    now = int(now_ts if now_ts is not None else time.time())
    token = secrets.token_urlsafe(12)

    def _mut(
        state: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any] | None, str | None]:
        lane = state.get(lane_key)
        if not isinstance(lane, dict):
            return None, None, f"write lane '{lane_key}' not found"
        _refresh_lane_status(lane, now)
        if lane.get("status") != "active" or not bool(lane.get("approved")):
            state[lane_key] = lane
            return (
                None,
                dict(lane),
                f"write lane is not active (status={lane.get('status')})",
            )
        if normalized_target not in set(lane.get("targets") or []):
            return None, dict(lane), "target is outside the approved write lane"

        hours_error = lane_active_hours_error(lane, now_ts=now)
        if hours_error:
            return None, dict(lane), hours_error

        total_count = int(lane.get("sent_count") or 0)
        max_total = int(lane.get("max_messages") or 0)
        if total_count >= max_total:
            lane["status"] = "exhausted"
            lane["last_error"] = "lane message quota exhausted"
            state[lane_key] = lane
            return None, dict(lane), lane["last_error"]

        target_counts = dict(lane.get("target_sent_counts") or {})
        target_count = int(target_counts.get(normalized_target) or 0)
        max_per_target = int(lane.get("max_messages_per_target") or 0)
        if target_count >= max_per_target:
            return None, dict(lane), "lane per-target message quota exhausted"

        last_sent = int(
            dict(lane.get("last_sent_at_by_target") or {}).get(normalized_target) or 0
        )
        min_interval = int(lane.get("min_interval_sec") or 0)
        if last_sent and now - last_sent < min_interval:
            retry_after = min_interval - (now - last_sent)
            return (
                None,
                dict(lane),
                f"lane min interval not reached; retry after {retry_after}s",
            )

        lock_until = int(lane.get("send_lock_until_ts") or 0)
        if lock_until > now:
            return None, dict(lane), f"lane send is locked until {lock_until}"

        lane["send_lock_token"] = token
        lane["send_lock_until_ts"] = now + max(10, int(LANE_SEND_LOCK_SEC))
        state[lane_key] = lane
        return token, dict(lane), None

    return update_json_dict(LANE_FILE, _mut, root_key="lanes")


def _finalize_lane_send(
    lane_id: str,
    *,
    lock_token: str,
    target: str,
    action_hash: str,
    message_len: int,
    success: bool,
    error: str | None = None,
    now_ts: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    lane_key = str(lane_id or "").strip()
    normalized_target = _normalize_target(target)
    now = int(now_ts if now_ts is not None else time.time())

    def _mut(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        lane = state.get(lane_key)
        if not isinstance(lane, dict):
            return None, f"write lane '{lane_key}' not found"
        if str(lane.get("send_lock_token") or "") != str(lock_token or ""):
            return dict(lane), "lane send lock was lost before finalization"

        lane["send_lock_token"] = None
        lane["send_lock_until_ts"] = now
        outcome = "sent" if success else "send_failed"
        event: dict[str, Any] = {
            "event_id": f"evt_{secrets.token_urlsafe(6)}",
            "ts": now,
            "target": normalized_target,
            "outcome": outcome,
            "action_hash": action_hash,
            "message_len": int(message_len),
        }
        if success:
            lane["sent_count"] = int(lane.get("sent_count") or 0) + 1
            target_counts = dict(lane.get("target_sent_counts") or {})
            target_counts[normalized_target] = (
                int(target_counts.get(normalized_target) or 0) + 1
            )
            lane["target_sent_counts"] = target_counts
            last_sent = dict(lane.get("last_sent_at_by_target") or {})
            last_sent[normalized_target] = now
            lane["last_sent_at_by_target"] = last_sent
            lane["last_error"] = None
            _refresh_lane_status(lane, now)
        else:
            lane["last_error"] = str(error or "send_message failed")[:300]
            event["error"] = lane["last_error"]
        _append_lane_audit(lane, event)
        state[lane_key] = lane
        return dict(lane), None

    return update_json_dict(LANE_FILE, _mut, root_key="lanes")


async def _current_action_account() -> tuple[dict[str, Any] | None, str | None]:
    auth = await ctx.auth_status()
    if not auth.get("authorized"):
        return None, str(
            auth.get("error") or "Telegram actions session is unauthorized"
        )
    account = auth.get("account")
    if not isinstance(account, dict) or account.get("id") is None:
        return None, "Telegram actions session has no stable account id"
    return account, None


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
async def tg_create_managed_bot(
    manager_bot: str,
    name: str,
    username: str,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
    via_deeplink: bool = False,
) -> dict:
    """Create a Telegram managed bot through an allowlisted manager bot."""
    clean_manager = str(manager_bot or "").strip()
    clean_name = str(name or "").strip()
    clean_username = str(username or "").strip().lstrip("@")
    if not clean_manager:
        return _blocked("manager_bot is empty")
    if not clean_name:
        return _blocked("name is empty")
    if not clean_username:
        return _blocked("username is empty")

    can_run, error = _check_action_preconditions(
        clean_manager,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    action_hash = _hash_payload(
        {
            "action": "create_managed_bot",
            "manager_bot": _normalize_target(clean_manager),
            "name": clean_name,
            "username": clean_username.lower(),
            "via_deeplink": bool(via_deeplink),
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
        manager_entity = await manager._resolve_target_entity(clean_manager)
        username_available = await safe_call(
            ctx.client,
            CheckManagedBotUsernameRequest(username=clean_username),
            operation_type="api",
        )
    except Exception as exc:
        return _blocked(
            str(exc),
            manager_bot=clean_manager,
            username=clean_username,
            action_hash=action_hash,
        )

    preview = {
        "success": True,
        "dry_run": True,
        "manager_bot": clean_manager,
        "manager_id": getattr(manager_entity, "id", None),
        "name": clean_name,
        "username": clean_username,
        "username_available": bool(username_available),
        "via_deeplink": bool(via_deeplink),
        "action_hash": action_hash,
        "confirmation_text_required": (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        ),
    }
    if approval_meta:
        preview.update(approval_meta)
    if dry_run:
        return preview

    if not bool(username_available):
        return _blocked(
            "Telegram reports that the managed-bot username is unavailable",
            manager_bot=clean_manager,
            username=clean_username,
            action_hash=action_hash,
        )

    try:
        created = await safe_call(
            ctx.client,
            CreateManagedBotRequest(
                name=clean_name,
                username=clean_username,
                manager_id=manager_entity,
                via_deeplink=bool(via_deeplink),
            ),
            operation_type="api",
        )
    except Exception as exc:
        # Telegram may return a newer User constructor than the pinned
        # Telethon understands. At that point the mutation already succeeded;
        # verify the exact username read-only before recording success.
        if "Could not find a matching Constructor ID" in str(exc):
            try:
                verified = await manager.resolve_username(clean_username)
            except Exception:
                verified = None
            if (
                isinstance(verified, dict)
                and str(verified.get("username") or "").lower()
                == clean_username.lower()
                and bool(verified.get("is_bot"))
            ):
                _mark_action_executed(action_hash)
                return {
                    "success": False,
                    "outcome_unknown": True,
                    "error": "bot exists, but creation and manager ownership could not be verified",
                    "dry_run": False,
                    "manager_bot": clean_manager,
                    "manager_id": getattr(manager_entity, "id", None),
                    "observed_bot": {
                        "id": verified.get("id"),
                        "username": verified.get("username"),
                        "name": verified.get("first_name"),
                        "is_bot": True,
                    },
                    "response_parse_warning": "new Telegram User constructor",
                    "action_hash": action_hash,
                }
        return _blocked(
            str(exc),
            manager_bot=clean_manager,
            username=clean_username,
            action_hash=action_hash,
        )

    _mark_action_executed(action_hash)
    return {
        "success": True,
        "dry_run": False,
        "manager_bot": clean_manager,
        "manager_id": getattr(manager_entity, "id", None),
        "created_bot": {
            "id": getattr(created, "id", None),
            "username": getattr(created, "username", None),
            "name": getattr(created, "first_name", None),
            "is_bot": bool(getattr(created, "bot", False)),
        },
        "action_hash": action_hash,
    }


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
async def tg_click_inline_button(
    group: str,
    message_id: int,
    button_text: str = "",
    row: int = -1,
    col: int = -1,
    button_data_b64: str = "",
    exact_text: bool = True,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Click an inline callback button on a bot message with ActionMCP gates."""
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

    try:
        normalized_row = int(row)
        normalized_col = int(col)
    except Exception:
        return _blocked("row and col must be integers")

    manager = await ctx.get_manager()
    try:
        entity = await manager._resolve_target_entity(group)
        msg = await safe_call(
            ctx.client.get_messages,
            entity,
            ids=normalized_message_id,
            operation_type="api",
        )
    except Exception as exc:
        return _blocked(str(exc))

    if not msg:
        return _blocked(
            f"message_id {normalized_message_id} not found",
            target=group,
            message_id=normalized_message_id,
        )

    options = _message_button_options(msg)
    selected, select_error = _select_button_option(
        options,
        button_text=button_text,
        row=normalized_row,
        col=normalized_col,
        button_data_b64=button_data_b64,
        exact_text=bool(exact_text),
    )
    public_options = _public_button_options(options)
    if select_error or not selected:
        return _blocked(
            select_error or "button selector failed",
            target=group,
            message_id=normalized_message_id,
            available_buttons=public_options,
        )

    selected_public = dict(selected)
    selected_public.pop("_button", None)
    selected_data = _button_data_bytes(selected.get("_button"))
    if selected_data is None:
        return _blocked(
            "selected button is not an inline callback button",
            target=group,
            message_id=normalized_message_id,
            selected_button=selected_public,
            available_buttons=public_options,
        )

    action_hash = _hash_payload(
        {
            "action": "click_inline_button",
            "target": _normalize_target(group),
            "message_id": normalized_message_id,
            "button_data_b64": base64.b64encode(selected_data).decode("ascii"),
            "button_text": str(selected.get("text") or ""),
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
            "message_id": normalized_message_id,
            "selected_button": selected_public,
            "available_buttons": public_options,
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

    from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

    try:
        response = await safe_call(
            ctx.client,
            GetBotCallbackAnswerRequest(
                peer=entity,
                msg_id=normalized_message_id,
                data=selected_data,
            ),
            operation_type="group_msg",
        )
    except Exception as exc:
        return _blocked(
            str(exc),
            target=group,
            message_id=normalized_message_id,
            selected_button=selected_public,
            action_hash=action_hash,
        )

    _mark_action_executed(action_hash)
    return {
        "success": True,
        "dry_run": False,
        "target": group,
        "message_id": normalized_message_id,
        "selected_button": selected_public,
        "response_type": type(response).__name__,
        "response_message": getattr(response, "message", None),
        "response_url": getattr(response, "url", None),
        "response_alert": getattr(response, "alert", None),
        "action_hash": action_hash,
    }


@mcp.tool()
async def tg_run_bot_steps(
    group: str,
    steps: list[dict[str, Any]],
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Run a pre-approved sequence of bot send/click steps in one allowlisted dialog."""
    can_run, error = _check_action_preconditions(
        group,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_text=confirmation_text,
    )
    if not can_run:
        return _blocked(error or "preconditions failed")

    normalized_steps, step_error = _normalize_bot_steps(steps)
    if step_error:
        return _blocked(step_error)

    action_hash = _hash_payload(
        {
            "action": "run_bot_steps",
            "target": _normalize_target(group),
            "steps": _public_bot_steps(normalized_steps),
        }
    )

    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    preview_payload = {
        "success": True,
        "dry_run": True,
        "target": group,
        "step_count": len(normalized_steps),
        "steps": _public_bot_steps(normalized_steps),
        "action_hash": action_hash,
        "confirmation_text_required": (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        ),
    }
    if approval_meta:
        preview_payload.update(approval_meta)
    if dry_run:
        return preview_payload

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
    try:
        entity = await manager._resolve_target_entity(group)
    except Exception as exc:
        return _blocked(str(exc), target=group, action_hash=action_hash)

    from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

    results: list[dict[str, Any]] = []
    for index, step in enumerate(normalized_steps):
        kind = step.get("type")
        if kind == "wait":
            await asyncio.sleep(float(step.get("wait_after_sec") or 0))
            results.append({"index": index, "type": kind, "success": True})
            continue

        if kind == "send_message":
            try:
                sent = await safe_call(
                    ctx.client.send_message,
                    entity,
                    str(step.get("text") or ""),
                    operation_type="group_msg",
                )
            except Exception as exc:
                return _blocked(
                    str(exc),
                    target=group,
                    action_hash=action_hash,
                    completed_steps=results,
                    failed_step=index,
                )
            results.append(
                {
                    "index": index,
                    "type": kind,
                    "success": True,
                    "message_id": getattr(sent, "id", None),
                    "message_len": len(str(step.get("text") or "")),
                }
            )
            await asyncio.sleep(float(step.get("wait_after_sec") or 0))
            continue

        try:
            msg, selected, inspected = await _find_button_for_step(entity, step)
        except Exception as exc:
            return _blocked(
                str(exc),
                target=group,
                action_hash=action_hash,
                completed_steps=results,
                failed_step=index,
            )

        selected_public = dict(selected)
        selected_public.pop("_button", None)
        selected_data = _button_data_bytes(selected.get("_button"))
        if selected_data is None:
            return _blocked(
                "selected button is not an inline callback button",
                target=group,
                action_hash=action_hash,
                completed_steps=results,
                failed_step=index,
                selected_button=selected_public,
            )

        callback_error = None
        callback_success = True
        try:
            response = await safe_call(
                ctx.client,
                GetBotCallbackAnswerRequest(
                    peer=entity,
                    msg_id=int(getattr(msg, "id")),
                    data=selected_data,
                ),
                operation_type="group_msg",
                timeout=float(step.get("callback_timeout_sec") or 8.0),
            )
            response_payload = {
                "response_type": type(response).__name__,
                "response_message": getattr(response, "message", None),
                "response_url": getattr(response, "url", None),
                "response_alert": getattr(response, "alert", None),
            }
        except Exception as exc:
            response_payload = {}
            callback_error = str(exc)
            callback_success = False
            if not (
                bool(step.get("continue_on_callback_timeout", True))
                and _is_callback_timeout_error(exc)
            ):
                return _blocked(
                    str(exc),
                    target=group,
                    action_hash=action_hash,
                    completed_steps=results,
                    failed_step=index,
                    selected_button=selected_public,
                )

        results.append(
            {
                "index": index,
                "type": kind,
                "success": callback_success,
                "nonfatal_callback_error": callback_error,
                "message_id": getattr(msg, "id", None),
                "selected_button": selected_public,
                "inspected_message_count": len(inspected),
                **response_payload,
            }
        )
        await asyncio.sleep(float(step.get("wait_after_sec") or 0))

    _mark_action_executed(action_hash)
    return {
        "success": True,
        "dry_run": False,
        "target": group,
        "step_count": len(normalized_steps),
        "steps_completed": len(results),
        "results": results,
        "action_hash": action_hash,
    }


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
async def tg_pin_message(
    group: str,
    message_id: int,
    notify: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Pin one message with policy gates and idempotency."""
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
    except (TypeError, ValueError):
        return _blocked("message_id must be a positive integer")
    if normalized_message_id <= 0:
        return _blocked("message_id must be a positive integer")

    action_hash = _hash_payload(
        {
            "action": "pin_message",
            "target": _normalize_target(group),
            "message_id": normalized_message_id,
            "notify": bool(notify),
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
            "message_id": normalized_message_id,
            "notify": bool(notify),
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
    pinned = await manager.pin_message(
        group,
        normalized_message_id,
        notify=bool(notify),
    )
    if pinned:
        _mark_action_executed(action_hash)
        return {
            "success": True,
            "target": group,
            "message_id": normalized_message_id,
            "notify": bool(notify),
            "action_hash": action_hash,
        }

    return {
        "success": False,
        "target": group,
        "message_id": normalized_message_id,
        "action_hash": action_hash,
        "error": "pin_message failed (see server logs for details)",
    }


@mcp.tool()
async def tg_create_write_lane(
    name: str,
    purpose: str,
    targets: list[str],
    ttl_sec: int = 3600,
    max_messages: int = 20,
    max_messages_per_target: int = 10,
    min_interval_sec: int = 300,
    max_message_len: int = 1000,
    allow_links: bool = False,
    active_hours_start: str = "",
    active_hours_end: str = "",
    active_timezone: str = "",
) -> dict:
    """Create an inert send-message lane and return its exact approval preview."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    lane, error = create_write_lane_record(
        name=name,
        purpose=purpose,
        targets=list(targets or []),
        ttl_sec=ttl_sec,
        max_messages=max_messages,
        max_messages_per_target=max_messages_per_target,
        min_interval_sec=min_interval_sec,
        max_message_len=max_message_len,
        allow_links=allow_links,
        active_hours_start=active_hours_start,
        active_hours_end=active_hours_end,
        active_timezone=active_timezone,
        max_ttl_sec=LANE_MAX_TTL_SEC,
        max_targets=LANE_MAX_TARGETS,
        max_total_messages=LANE_MAX_MESSAGES,
        min_allowed_interval_sec=LANE_MIN_INTERVAL_SEC,
        global_max_message_len=MAX_MESSAGE_LEN,
        approval_ttl_sec=LANE_APPROVAL_TTL_SEC,
        check_target_allowed=_check_target_allowed,
    )
    if lane is None:
        return _blocked(error or "write lane validation failed")

    def _mut(state: dict[str, Any]) -> None:
        state[str(lane["id"])] = lane

    update_json_dict(LANE_FILE, _mut, root_key="lanes")
    approval_meta = _issue_approval(str(lane["scope_hash"]))
    result = {
        "success": True,
        **summarize_write_lane(lane),
        "scope_hash": lane["scope_hash"],
        "confirmation_text_required": (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        ),
        "next_step": (
            "Show this exact scope to the user, then call tg_approve_write_lane "
            "with confirmation_text and approval_code. The lane cannot write yet."
        ),
    }
    result.update(approval_meta)
    return result


@mcp.tool()
async def tg_approve_write_lane(
    lane_id: str,
    confirmation_text: str,
    approval_code: str,
) -> dict:
    """Activate one immutable write lane with one explicit human approval."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    lane, error = _get_write_lane(lane_id)
    if lane is None:
        return _blocked(error or "write lane not found")
    if lane.get("status") != "pending_approval":
        return _blocked(
            f"write lane cannot be approved from status={lane.get('status')}",
            **summarize_write_lane(lane),
        )

    ok, confirmation_error = _validate_confirmation_text(
        confirmation_text, dry_run=False
    )
    if not ok:
        return _blocked(confirmation_error or "confirmation_text validation failed")

    expected_scope_hash = lane_scope_hash(lane)
    if str(lane.get("scope_hash") or "") != expected_scope_hash:
        return _blocked("write lane scope changed after preview; create a fresh lane")

    account, account_error = await _current_action_account()
    if account is None:
        return _blocked(account_error or "could not bind write lane to account")

    # Verify the live Telegram identity before consuming the one-time code. A
    # transient auth/session failure must not burn a human approval.
    approval_ok, approval_error = _consume_approval(expected_scope_hash, approval_code)
    if not approval_ok:
        return _blocked(approval_error or "write lane approval code was rejected")

    now = int(time.time())
    lane_key = str(lane_id or "").strip()

    def _mut(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        current = state.get(lane_key)
        if not isinstance(current, dict):
            return None, f"write lane '{lane_key}' not found"
        _refresh_lane_status(current, now)
        if current.get("status") != "pending_approval":
            return (
                dict(current),
                f"write lane cannot be approved from status={current.get('status')}",
            )
        if lane_scope_hash(current) != expected_scope_hash:
            return dict(current), "write lane scope changed after preview"
        current["approved"] = True
        current["status"] = "active"
        current["approved_at_ts"] = now
        current["expires_at_ts"] = now + int(current.get("ttl_sec") or 0)
        current["bound_account_id"] = account.get("id")
        current["bound_username"] = str(account.get("username") or "").strip().lower()
        current["last_error"] = None
        _append_lane_audit(
            current,
            {
                "event_id": f"evt_{secrets.token_urlsafe(6)}",
                "ts": now,
                "outcome": "approved",
            },
        )
        state[lane_key] = current
        return dict(current), None

    approved_lane, state_error = update_json_dict(LANE_FILE, _mut, root_key="lanes")
    if approved_lane is None or state_error:
        return _blocked(state_error or "failed to activate write lane")
    return {
        "success": True,
        **summarize_write_lane(approved_lane),
        "scope_hash": approved_lane.get("scope_hash"),
        "delegated_actions": ["send_message"],
        "next_step": (
            "Use tg_send_message_with_lane. Per-message confirmation is waived only "
            "inside this exact scope until expiry, exhaustion, or revoke."
        ),
    }


@mcp.tool()
async def tg_get_write_lane(lane_id: str, include_audit: bool = False) -> dict:
    """Inspect one lane without changing its authority."""
    lane, error = _get_write_lane(lane_id)
    if lane is None:
        return _blocked(error or "write lane not found")
    result = {"success": True, **summarize_write_lane(lane)}
    if include_audit:
        result["audit"] = list(lane.get("audit") or [])[-50:]
    return result


@mcp.tool()
async def tg_list_write_lanes(status: str = "") -> dict:
    """List compact lane summaries, optionally filtered by status."""
    wanted = str(status or "").strip().lower()
    now = int(time.time())

    def _mut(state: dict[str, Any]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for lane_id, lane in state.items():
            if not isinstance(lane, dict):
                continue
            _refresh_lane_status(lane, now)
            state[str(lane_id)] = lane
            if wanted and str(lane.get("status") or "").lower() != wanted:
                continue
            summaries.append(summarize_write_lane(lane))
        summaries.sort(
            key=lambda item: int(item.get("created_at_ts") or 0), reverse=True
        )
        return summaries

    lanes = update_json_dict(LANE_FILE, _mut, root_key="lanes")
    return {"success": True, "count": len(lanes), "lanes": lanes}


@mcp.tool()
async def tg_revoke_write_lane(lane_id: str, reason: str = "") -> dict:
    """Immediately revoke a lane; revocation never needs write approval."""
    lane_key = str(lane_id or "").strip()
    if not lane_key:
        return _blocked("lane_id is empty")
    now = int(time.time())
    clean_reason = str(reason or "").strip()[:300]

    def _mut(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        lane = state.get(lane_key)
        if not isinstance(lane, dict):
            return None, f"write lane '{lane_key}' not found"
        if lane.get("status") == "revoked":
            return dict(lane), None
        lane["approved"] = False
        lane["status"] = "revoked"
        lane["revoked_at_ts"] = now
        lane["revoked_reason"] = clean_reason or "revoked by operator"
        # Keep an existing in-flight token so its eventual result can still be
        # finalized and audited. Status=revoked blocks every new acquisition.
        _append_lane_audit(
            lane,
            {
                "event_id": f"evt_{secrets.token_urlsafe(6)}",
                "ts": now,
                "outcome": "revoked",
                "reason": lane["revoked_reason"],
            },
        )
        state[lane_key] = lane
        return dict(lane), None

    lane, error = update_json_dict(LANE_FILE, _mut, root_key="lanes")
    if lane is None:
        return _blocked(error or "write lane not found")
    return {"success": True, **summarize_write_lane(lane)}


@mcp.tool()
async def tg_send_message_with_lane(
    lane_id: str,
    group: str,
    message_text: str,
    dry_run: bool = True,
) -> dict:
    """Send text under one active, account-bound, time-limited write lane."""
    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")

    lane, lane_error = _get_write_lane(lane_id)
    if lane is None:
        return _blocked(lane_error or "write lane not found")
    if lane.get("status") != "active" or not bool(lane.get("approved")):
        return _blocked(
            f"write lane is not active (status={lane.get('status')})",
            **summarize_write_lane(lane),
        )

    allowed, allowlist_error = _check_target_allowed(group)
    if not allowed:
        _record_lane_attempt(
            lane_id,
            target=group,
            outcome="blocked_allowlist",
            error=allowlist_error,
        )
        return _blocked(allowlist_error or "target is blocked by allowlist")

    normalized_target = _normalize_target(group)
    if normalized_target not in set(lane.get("targets") or []):
        error = "target is outside the approved write lane"
        _record_lane_attempt(
            lane_id, target=group, outcome="blocked_scope", error=error
        )
        return _blocked(error, **summarize_write_lane(lane))

    clean_text, message_error = validate_lane_message(lane, message_text)
    if clean_text is None:
        _record_lane_attempt(
            lane_id,
            target=group,
            outcome="blocked_content",
            error=message_error,
        )
        return _blocked(message_error or "message policy blocked send")

    account, account_error = await _current_action_account()
    if account is None:
        _record_lane_attempt(
            lane_id,
            target=group,
            outcome="blocked_account",
            error=account_error,
        )
        return _blocked(account_error or "could not verify Telegram account")
    if account.get("id") != lane.get("bound_account_id"):
        error = "current Telegram account does not match the lane-bound account"
        _record_lane_attempt(
            lane_id, target=group, outcome="blocked_account", error=error
        )
        return _blocked(error)

    action_hash = _hash_payload(
        {
            "action": "send_message",
            "target": normalized_target,
            "text": clean_text,
        }
    )
    duplicate, retry_after_sec = _check_recent_duplicate(action_hash)
    if duplicate:
        error = "Duplicate action blocked by idempotency window."
        _record_lane_attempt(
            lane_id,
            target=group,
            outcome="blocked_duplicate",
            action_hash=action_hash,
            message_len=len(clean_text),
            error=error,
        )
        return {
            "success": False,
            "duplicate_blocked": True,
            "retry_after_sec": retry_after_sec,
            "lane_id": lane_id,
            "target": group,
            "action_hash": action_hash,
            "error": error,
        }

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "lane_id": lane_id,
            "target": group,
            "message_len": len(clean_text),
            "action_hash": action_hash,
            "lane": summarize_write_lane(lane),
            "next_step": "Call again with dry_run=false to consume lane quota and send.",
        }

    manager = await ctx.get_manager()
    lock_token, locked_lane, lock_error = _acquire_lane_send(lane_id, target=group)
    if lock_token is None:
        _record_lane_attempt(
            lane_id,
            target=group,
            outcome="blocked_quota_or_lock",
            action_hash=action_hash,
            message_len=len(clean_text),
            error=lock_error,
        )
        return _blocked(
            lock_error or "write lane send could not acquire lease",
            **(summarize_write_lane(locked_lane) if locked_lane else {}),
        )

    sent = False
    send_error: str | None = None
    try:
        sent = bool(await manager.send_message(group, clean_text))
        if not sent:
            send_error = "send_message failed (see server logs for details)"
    except Exception as exc:
        send_error = str(exc)

    # Once Telegram reports success, reserve the payload immediately. Even if
    # lane finalization later fails, an automatic retry must not duplicate it.
    if sent:
        _mark_action_executed(action_hash)

    finalized_lane, finalize_error = _finalize_lane_send(
        lane_id,
        lock_token=lock_token,
        target=group,
        action_hash=action_hash,
        message_len=len(clean_text),
        success=sent,
        error=send_error,
    )
    if finalize_error:
        return _blocked(
            finalize_error,
            lane_id=lane_id,
            target=group,
            action_hash=action_hash,
            telegram_send_result=sent,
        )
    if sent:
        return {
            "success": True,
            "lane_id": lane_id,
            "target": group,
            "message_len": len(clean_text),
            "action_hash": action_hash,
            "lane": summarize_write_lane(finalized_lane or lane),
        }
    return _blocked(
        send_error or "send_message failed",
        lane_id=lane_id,
        target=group,
        action_hash=action_hash,
        lane=summarize_write_lane(finalized_lane or lane),
    )


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
async def tg_delete_contacts(
    users: list[str],
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Delete Telegram contacts by user id/username with policy gates."""
    normalized_users = []
    seen = set()
    for raw in users or []:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized_users.append(value)
    if not normalized_users:
        return _blocked("users list is empty")

    blocked_targets = []
    for user in normalized_users:
        allowed, error = _check_target_allowed(user)
        if not allowed:
            blocked_targets.append({"user": user, "error": error})
    if blocked_targets:
        return _blocked(
            "one or more users are not in allowlist", blocked_targets=blocked_targets
        )

    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")
    if not dry_run and not confirm:
        return _blocked(
            "Execution blocked: set confirm=true to run destructive action. "
            "Use dry_run=true to preview safely."
        )
    ok, err = _validate_confirmation_text(confirmation_text, dry_run=dry_run)
    if not ok:
        return _blocked(err or "confirmation_text validation failed")

    action_hash = _hash_payload(
        {
            "action": "delete_contacts",
            "users": sorted(_normalize_target(user) for user in normalized_users),
        }
    )
    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    await ctx.get_manager()
    input_users = []
    resolved = []
    for user in normalized_users:
        target: str | int = int(user) if user.isdigit() else user
        entity = await ctx.client.get_input_entity(target)
        input_users.append(entity)
        resolved.append({"user": user, "input_type": type(entity).__name__})

    result = {
        "success": True,
        "action": "delete_contacts",
        "dry_run": dry_run,
        "user_count": len(normalized_users),
        "users": normalized_users,
        "resolved": resolved,
        "action_hash": action_hash,
        "confirmation_text_required": (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        ),
    }
    if approval_meta:
        result.update(approval_meta)
    if dry_run:
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

    from telethon.tl.functions.contacts import DeleteContactsRequest

    await ctx.client(DeleteContactsRequest(id=input_users))
    _mark_action_executed(action_hash)
    result["dry_run"] = False
    return result


@mcp.tool()
async def tg_delete_contacts_by_phones(
    phones: list[str],
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_text: str = "",
    approval_code: str = "",
    force_resend: bool = False,
) -> dict:
    """Delete saved/imported Telegram contacts by exact phone numbers."""
    normalized_phones = []
    seen = set()
    for raw in phones or []:
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if not digits or digits in seen:
            continue
        seen.add(digits)
        normalized_phones.append(digits)
    if not normalized_phones:
        return _blocked("phones list is empty")

    blocked_targets = []
    for phone in normalized_phones:
        allowed, error = _check_target_allowed(phone)
        if not allowed:
            blocked_targets.append({"phone": phone, "error": error})
    if blocked_targets:
        return _blocked(
            "one or more phones are not in allowlist", blocked_targets=blocked_targets
        )

    if SAFE_STARTUP_BLOCK_REASON:
        return _blocked(SAFE_STARTUP_BLOCK_REASON)
    if not ACTIONS_ENABLED:
        return _blocked("Actions are disabled. Set TG_ACTIONS_ENABLED=1.")
    if not dry_run and not confirm:
        return _blocked(
            "Execution blocked: set confirm=true to run destructive action. "
            "Use dry_run=true to preview safely."
        )
    ok, err = _validate_confirmation_text(confirmation_text, dry_run=dry_run)
    if not ok:
        return _blocked(err or "confirmation_text validation failed")

    action_hash = _hash_payload(
        {
            "action": "delete_contacts_by_phones",
            "phones": sorted(normalized_phones),
        }
    )
    approval_ok, approval_error, approval_meta = _approval_gate(
        action_hash=action_hash,
        dry_run=dry_run,
        approval_code=approval_code,
    )
    if not approval_ok:
        return _blocked(approval_error or "approval gate blocked")

    result = {
        "success": True,
        "action": "delete_contacts_by_phones",
        "dry_run": dry_run,
        "phone_count": len(normalized_phones),
        "phones": normalized_phones,
        "action_hash": action_hash,
        "confirmation_text_required": (
            CONFIRMATION_PHRASE if REQUIRE_CONFIRMATION_TEXT else None
        ),
    }
    if approval_meta:
        result.update(approval_meta)
    if dry_run:
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

    await ctx.get_manager()
    from telethon.tl.functions.contacts import DeleteByPhonesRequest

    api_result = await ctx.client(DeleteByPhonesRequest(phones=normalized_phones))
    _mark_action_executed(action_hash)
    result["dry_run"] = False
    result["api_result"] = bool(api_result)
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
    now = int(time.time())

    def _lane_counts(state: dict[str, Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for lane_id, lane in state.items():
            if not isinstance(lane, dict):
                continue
            _refresh_lane_status(lane, now)
            state[str(lane_id)] = lane
            status = str(lane.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    lane_status_counts = update_json_dict(LANE_FILE, _lane_counts, root_key="lanes")
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
        "write_lane_file": str(LANE_FILE),
        "write_lane_status_counts": lane_status_counts,
        "write_lane_max_ttl_sec": LANE_MAX_TTL_SEC,
        "write_lane_approval_ttl_sec": LANE_APPROVAL_TTL_SEC,
        "write_lane_max_targets": LANE_MAX_TARGETS,
        "write_lane_max_messages": LANE_MAX_MESSAGES,
        "write_lane_min_interval_sec": LANE_MIN_INTERVAL_SEC,
        "write_lane_send_lock_sec": LANE_SEND_LOCK_SEC,
        "write_lane_audit_max_records": LANE_AUDIT_MAX_RECORDS,
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
        "recommended_write_lane_flow": [
            "1) Create immutable scope with tg_create_write_lane.",
            "2) Show targets, purpose, TTL, quotas, link policy, and approval_code.",
            "3) Call tg_approve_write_lane once with exact confirmation_text + approval_code.",
            "4) A monitor may call tg_send_message_with_lane(dry_run=false) only inside scope.",
            "5) Call tg_revoke_write_lane at any time; expiry and quotas fail closed.",
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
    # Transport is env-selectable so one shared HTTP instance can serve many MCP
    # clients: the actions profile claims the Telegram session exclusively, so a
    # stdio copy per client makes every client after the first fail to start.
    _transport = os.environ.get("TG_MCP_TRANSPORT", "stdio")
    if _transport != "stdio":
        mcp.settings.host = os.environ.get("TG_MCP_HTTP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("TG_MCP_HTTP_PORT", "8787"))
    mcp.run(transport=_transport)
