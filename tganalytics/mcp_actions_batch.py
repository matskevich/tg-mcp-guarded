"""Batch assembly and summary helpers for Action MCP."""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable

from mcp_actions_policy import hash_payload, normalize_target


def summarize_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """Build compact progress summary for batch state."""
    actions = batch.get("actions", [])
    counts = {
        "pending_count": 0,
        "success_count": 0,
        "already_member_count": 0,
        "failed_count": 0,
        "blocked_rights_count": 0,
        "blocked_policy_count": 0,
    }
    for action in actions:
        status = str(action.get("status", "pending"))
        if status == "pending":
            counts["pending_count"] += 1
        elif status == "success":
            counts["success_count"] += 1
        elif status == "already_member":
            counts["already_member_count"] += 1
        elif status == "blocked_rights":
            counts["blocked_rights_count"] += 1
        elif status == "blocked_policy":
            counts["blocked_policy_count"] += 1
        else:
            counts["failed_count"] += 1

    return {
        "batch_id": batch.get("id"),
        "batch_type": batch.get("type"),
        "status": batch.get("status"),
        "approved": bool(batch.get("approved", False)),
        "approval_valid_until_ts": batch.get("approved_until_ts"),
        "run_lock_owner": batch.get("run_lock_owner"),
        "run_lock_until_ts": batch.get("run_lock_until_ts"),
        "user": batch.get("user"),
        "created_at_ts": batch.get("created_at_ts"),
        "approved_at_ts": batch.get("approved_at_ts"),
        "expires_at_ts": batch.get("expires_at_ts"),
        "total": len(actions),
        **counts,
    }


def create_add_member_batch_record(
    *,
    user: str,
    groups: list[str],
    note: str,
    ttl_hours: int,
    check_target_allowed: Callable[[str], tuple[bool, str | None]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Create canonical batch record for add-member workflow."""
    normalized_user = str(user).strip()
    unique_groups: list[str] = []
    seen = set()
    for group in groups:
        g = str(group).strip()
        if not g or g in seen:
            continue
        seen.add(g)
        unique_groups.append(g)

    blocked_targets: list[dict[str, str]] = []
    actions: list[dict[str, Any]] = []
    user_key = normalized_user.lower()

    for group in unique_groups:
        allowed, error = check_target_allowed(group)
        action_hash = hash_payload(
            {
                "action": "add_member",
                "target": normalize_target(group),
                "user": user_key,
            }
        )
        if not allowed:
            blocked_targets.append({"group": group, "error": error or "blocked"})
            actions.append(
                {
                    "group": group,
                    "action_hash": action_hash,
                    "status": "blocked_policy",
                    "attempts": 0,
                    "last_error": error,
                    "last_run_ts": None,
                }
            )
            continue

        actions.append(
            {
                "group": group,
                "action_hash": action_hash,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "last_run_ts": None,
            }
        )

    now = int(time.time())
    hours = max(1, int(ttl_hours))
    batch_id = f"batch_{secrets.token_urlsafe(7)}"
    batch = {
        "id": batch_id,
        "type": "add_member",
        "status": "pending_approval",
        "approved": False,
        "approved_until_ts": None,
        "run_lock_owner": None,
        "run_lock_until_ts": None,
        "note": (note or "").strip(),
        "user": normalized_user,
        "created_at_ts": now,
        "approved_at_ts": None,
        "expires_at_ts": now + (hours * 3600),
        "actions": actions,
        "last_run_ts": None,
        "last_error": None,
    }
    return batch, blocked_targets


def _unique_targets(raw_targets: list[str]) -> list[str]:
    unique: list[str] = []
    seen = set()
    for target in raw_targets:
        value = str(target).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def create_leave_dialog_batch_record(
    *,
    targets: list[str],
    note: str,
    ttl_hours: int,
    check_target_allowed: Callable[[str], tuple[bool, str | None]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Create canonical batch record for leaving many dialogs."""
    blocked_targets: list[dict[str, str]] = []
    actions: list[dict[str, Any]] = []
    for target in _unique_targets(targets):
        allowed, error = check_target_allowed(target)
        action_hash = hash_payload(
            {
                "action": "leave_dialog",
                "target": normalize_target(target),
            }
        )
        status = "pending" if allowed else "blocked_policy"
        if not allowed:
            blocked_targets.append({"group": target, "error": error or "blocked"})
        actions.append(
            {
                "group": target,
                "action_hash": action_hash,
                "status": status,
                "attempts": 0,
                "last_error": None if allowed else error,
                "last_run_ts": None,
            }
        )

    now = int(time.time())
    hours = max(1, int(ttl_hours))
    batch_id = f"batch_{secrets.token_urlsafe(7)}"
    batch = {
        "id": batch_id,
        "type": "leave_dialog",
        "status": "pending_approval",
        "approved": False,
        "approved_until_ts": None,
        "run_lock_owner": None,
        "run_lock_until_ts": None,
        "note": (note or "").strip(),
        "created_at_ts": now,
        "approved_at_ts": None,
        "expires_at_ts": now + (hours * 3600),
        "actions": actions,
        "last_run_ts": None,
        "last_error": None,
    }
    return batch, blocked_targets


def create_delete_messages_batch_record(
    *,
    targets: list[dict[str, Any]],
    note: str,
    ttl_hours: int,
    max_ids_per_action: int,
    revoke: bool,
    check_target_allowed: Callable[[str], tuple[bool, str | None]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Create canonical batch record for deleting message ids in many dialogs."""
    blocked_targets: list[dict[str, str]] = []
    actions: list[dict[str, Any]] = []
    chunk_size = max(1, int(max_ids_per_action or 100))

    for target_record in targets:
        group = str(
            target_record.get("group") or target_record.get("chat_id") or ""
        ).strip()
        if not group:
            continue
        raw_ids = target_record.get("message_ids") or []
        message_ids: list[int] = []
        seen_ids = set()
        for raw_id in raw_ids:
            try:
                message_id = int(raw_id)
            except Exception:
                continue
            if message_id <= 0 or message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            message_ids.append(message_id)
        if not message_ids:
            continue

        allowed, error = check_target_allowed(group)
        if not allowed:
            blocked_targets.append({"group": group, "error": error or "blocked"})

        for idx in range(0, len(message_ids), chunk_size):
            chunk = message_ids[idx : idx + chunk_size]
            action_hash = hash_payload(
                {
                    "action": "delete_messages",
                    "target": normalize_target(group),
                    "message_ids": chunk,
                    "revoke": bool(revoke),
                }
            )
            actions.append(
                {
                    "group": group,
                    "message_ids": chunk,
                    "message_count": len(chunk),
                    "action_hash": action_hash,
                    "status": "pending" if allowed else "blocked_policy",
                    "attempts": 0,
                    "last_error": None if allowed else error,
                    "last_run_ts": None,
                }
            )

    now = int(time.time())
    hours = max(1, int(ttl_hours))
    batch_id = f"batch_{secrets.token_urlsafe(7)}"
    batch = {
        "id": batch_id,
        "type": "delete_messages",
        "status": "pending_approval",
        "approved": False,
        "approved_until_ts": None,
        "run_lock_owner": None,
        "run_lock_until_ts": None,
        "note": (note or "").strip(),
        "created_at_ts": now,
        "approved_at_ts": None,
        "expires_at_ts": now + (hours * 3600),
        "revoke": bool(revoke),
        "actions": actions,
        "last_run_ts": None,
        "last_error": None,
    }
    return batch, blocked_targets
