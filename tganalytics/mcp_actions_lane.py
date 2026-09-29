"""Pure helpers for time-bounded Action MCP write lanes."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp_actions_policy import hash_payload, normalize_target

_LINK_RE = re.compile(
    r"(?:https?://|www\.|t\.me/|telegram\.me/|"
    r"(?<!@)\b(?:[a-z0-9-]+\.)+[a-z]{2,63}(?:[/?:#][^\s]*)?)",
    re.IGNORECASE,
)


def lane_scope_payload(lane: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable, approval-bound capability scope."""
    return {
        "version": int(lane.get("version") or 1),
        "lane_id": str(lane.get("id") or ""),
        "name": str(lane.get("name") or ""),
        "purpose": str(lane.get("purpose") or ""),
        "targets": list(lane.get("targets") or []),
        "ttl_sec": int(lane.get("ttl_sec") or 0),
        "max_messages": int(lane.get("max_messages") or 0),
        "max_messages_per_target": int(lane.get("max_messages_per_target") or 0),
        "min_interval_sec": int(lane.get("min_interval_sec") or 0),
        "max_message_len": int(lane.get("max_message_len") or 0),
        "allow_links": bool(lane.get("allow_links", False)),
        "active_hours_start": str(lane.get("active_hours_start") or ""),
        "active_hours_end": str(lane.get("active_hours_end") or ""),
        "active_timezone": str(lane.get("active_timezone") or ""),
        "created_at_ts": int(lane.get("created_at_ts") or 0),
        "approval_deadline_ts": int(lane.get("approval_deadline_ts") or 0),
    }


def lane_scope_hash(lane: dict[str, Any]) -> str:
    """Hash the exact scope shown to the approving human."""
    return hash_payload({"action": "approve_write_lane", **lane_scope_payload(lane)})


def summarize_write_lane(lane: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, secret-free lane status."""
    return {
        "lane_id": lane.get("id"),
        "name": lane.get("name"),
        "purpose": lane.get("purpose"),
        "status": lane.get("status"),
        "approved": bool(lane.get("approved", False)),
        "targets": list(lane.get("targets") or []),
        "created_at_ts": lane.get("created_at_ts"),
        "approved_at_ts": lane.get("approved_at_ts"),
        "approval_deadline_ts": lane.get("approval_deadline_ts"),
        "expires_at_ts": lane.get("expires_at_ts"),
        "ttl_sec": lane.get("ttl_sec"),
        "max_messages": lane.get("max_messages"),
        "max_messages_per_target": lane.get("max_messages_per_target"),
        "min_interval_sec": lane.get("min_interval_sec"),
        "max_message_len": lane.get("max_message_len"),
        "allow_links": bool(lane.get("allow_links", False)),
        "active_hours_start": lane.get("active_hours_start"),
        "active_hours_end": lane.get("active_hours_end"),
        "active_timezone": lane.get("active_timezone"),
        "sent_count": int(lane.get("sent_count") or 0),
        "target_sent_counts": dict(lane.get("target_sent_counts") or {}),
        "bound_username": lane.get("bound_username"),
        "revoked_at_ts": lane.get("revoked_at_ts"),
        "revoked_reason": lane.get("revoked_reason"),
        "last_error": lane.get("last_error"),
    }


def create_write_lane_record(
    *,
    name: str,
    purpose: str,
    targets: list[str],
    ttl_sec: int,
    max_messages: int,
    max_messages_per_target: int,
    min_interval_sec: int,
    max_message_len: int,
    allow_links: bool,
    active_hours_start: str,
    active_hours_end: str,
    active_timezone: str,
    max_ttl_sec: int,
    max_targets: int,
    max_total_messages: int,
    min_allowed_interval_sec: int,
    global_max_message_len: int,
    approval_ttl_sec: int,
    check_target_allowed: Callable[[str], tuple[bool, str | None]],
    now_ts: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate requested scope and build a pending, non-authoritative lane."""
    clean_name = str(name or "").strip()
    clean_purpose = str(purpose or "").strip()
    if not clean_name:
        return None, "lane name is empty"
    if len(clean_name) > 80:
        return None, "lane name is too long (max 80 chars)"
    if not clean_purpose:
        return None, "lane purpose is empty"
    if len(clean_purpose) > 500:
        return None, "lane purpose is too long (max 500 chars)"

    normalized_targets: list[str] = []
    seen: set[str] = set()
    for raw_target in targets or []:
        display_target = str(raw_target or "").strip()
        normalized = normalize_target(display_target)
        if not normalized or normalized in seen:
            continue
        allowed, error = check_target_allowed(display_target)
        if not allowed:
            return None, error or f"target '{display_target}' is blocked"
        seen.add(normalized)
        normalized_targets.append(normalized)

    if not normalized_targets:
        return None, "lane targets are empty"
    if len(normalized_targets) > int(max_targets):
        return (
            None,
            f"too many lane targets ({len(normalized_targets)} > {max_targets})",
        )

    requested_ttl = int(ttl_sec)
    if requested_ttl < 60:
        return None, "ttl_sec must be at least 60"
    if requested_ttl > int(max_ttl_sec):
        return None, f"ttl_sec exceeds policy maximum ({requested_ttl} > {max_ttl_sec})"

    requested_max_messages = int(max_messages)
    if requested_max_messages <= 0:
        return None, "max_messages must be > 0"
    if requested_max_messages > int(max_total_messages):
        return (
            None,
            "max_messages exceeds policy maximum "
            f"({requested_max_messages} > {max_total_messages})",
        )

    requested_per_target = int(max_messages_per_target)
    if requested_per_target <= 0:
        return None, "max_messages_per_target must be > 0"
    if requested_per_target > requested_max_messages:
        return None, "max_messages_per_target cannot exceed max_messages"

    requested_interval = int(min_interval_sec)
    if requested_interval < int(min_allowed_interval_sec):
        return (
            None,
            "min_interval_sec is below policy minimum "
            f"({requested_interval} < {min_allowed_interval_sec})",
        )

    requested_message_len = int(max_message_len)
    if requested_message_len <= 0:
        return None, "max_message_len must be > 0"
    if requested_message_len > int(global_max_message_len):
        return (
            None,
            "max_message_len exceeds Action MCP maximum "
            f"({requested_message_len} > {global_max_message_len})",
        )

    clean_start, start_minutes, hours_error = _parse_active_hour(
        active_hours_start, field_name="active_hours_start"
    )
    if hours_error:
        return None, hours_error
    clean_end, end_minutes, hours_error = _parse_active_hour(
        active_hours_end, field_name="active_hours_end"
    )
    if hours_error:
        return None, hours_error
    if bool(clean_start) != bool(clean_end):
        return None, "active_hours_start and active_hours_end must be provided together"
    clean_timezone = str(active_timezone or "").strip()
    if clean_start:
        if start_minutes == end_minutes:
            return None, "active hours start and end cannot be equal"
        if not clean_timezone:
            return None, "active_timezone is required when active hours are set"
        try:
            ZoneInfo(clean_timezone)
        except ZoneInfoNotFoundError:
            return None, f"active_timezone is not a valid IANA timezone: {clean_timezone}"
    else:
        clean_timezone = ""

    now = int(now_ts if now_ts is not None else time.time())
    lane_id = f"lane_{secrets.token_urlsafe(8)}"
    lane: dict[str, Any] = {
        "version": 1,
        "id": lane_id,
        "name": clean_name,
        "purpose": clean_purpose,
        "status": "pending_approval",
        "approved": False,
        "targets": normalized_targets,
        "created_at_ts": now,
        "approval_deadline_ts": now + max(60, int(approval_ttl_sec)),
        "approved_at_ts": None,
        "expires_at_ts": None,
        "ttl_sec": requested_ttl,
        "max_messages": requested_max_messages,
        "max_messages_per_target": requested_per_target,
        "min_interval_sec": requested_interval,
        "max_message_len": requested_message_len,
        "allow_links": bool(allow_links),
        "active_hours_start": clean_start,
        "active_hours_end": clean_end,
        "active_timezone": clean_timezone,
        "sent_count": 0,
        "target_sent_counts": {target: 0 for target in normalized_targets},
        "last_sent_at_by_target": {},
        "bound_account_id": None,
        "bound_username": None,
        "send_lock_token": None,
        "send_lock_until_ts": None,
        "revoked_at_ts": None,
        "revoked_reason": None,
        "last_error": None,
        "audit": [],
    }
    lane["scope_hash"] = lane_scope_hash(lane)
    return lane, None


def _parse_active_hour(value: str, *, field_name: str) -> tuple[str, int, str | None]:
    clean = str(value or "").strip()
    if not clean:
        return "", 0, None
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", clean)
    if not match:
        return "", 0, f"{field_name} must use 24-hour HH:MM format"
    hours, minutes = int(match.group(1)), int(match.group(2))
    return clean, hours * 60 + minutes, None


def lane_active_hours_error(
    lane: dict[str, Any], *, now_ts: int | None = None
) -> str | None:
    """Return a fail-closed error when now is outside the approved local window."""
    start = str(lane.get("active_hours_start") or "").strip()
    end = str(lane.get("active_hours_end") or "").strip()
    timezone_name = str(lane.get("active_timezone") or "").strip()
    if not start and not end and not timezone_name:
        return None
    clean_start, start_minutes, start_error = _parse_active_hour(
        start, field_name="active_hours_start"
    )
    clean_end, end_minutes, end_error = _parse_active_hour(
        end, field_name="active_hours_end"
    )
    if start_error or end_error or not clean_start or not clean_end or not timezone_name:
        return "write lane active-hours scope is invalid"
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return "write lane active timezone is invalid"
    current = datetime.fromtimestamp(
        int(now_ts if now_ts is not None else time.time()), tz=timezone
    )
    current_minutes = current.hour * 60 + current.minute
    if start_minutes < end_minutes:
        allowed = start_minutes <= current_minutes < end_minutes
    else:
        allowed = current_minutes >= start_minutes or current_minutes < end_minutes
    if allowed:
        return None
    return (
        "write lane is outside approved active hours "
        f"{start}-{end} {timezone_name}"
    )


def validate_lane_message(
    lane: dict[str, Any], message_text: str
) -> tuple[str | None, str | None]:
    """Validate text-only content against the approved lane scope."""
    clean_text = str(message_text or "").strip()
    if not clean_text:
        return None, "message_text is empty"
    max_len = int(lane.get("max_message_len") or 0)
    if max_len <= 0 or len(clean_text) > max_len:
        return None, f"message_text is too long ({len(clean_text)} > {max_len})"
    if not bool(lane.get("allow_links", False)) and _LINK_RE.search(clean_text):
        return None, "message_text contains a link but this lane does not allow links"
    return clean_text, None
