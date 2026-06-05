"""Policy helpers for Action MCP."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def normalize_target(group: str) -> str:
    """Normalize group identifier for allowlist checks."""
    value = str(group).strip()
    if value.startswith("@"):
        value = value[1:]
    return value.lower()


def parse_allowlist(raw: str) -> set[str]:
    """Parse comma-separated target identifiers into normalized set."""
    values = set()
    for chunk in raw.split(","):
        item = chunk.strip()
        if item:
            values.add(normalize_target(item))
    return values


def hash_payload(payload: dict[str, Any]) -> str:
    """Stable hash for action payload."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def detect_unsafe_defaults(
    *,
    env: Mapping[str, str],
    require_allowlist: bool,
    require_confirmation_text: bool,
    require_approval_code: bool,
    idempotency_enabled: bool,
) -> list[str]:
    """Return issues when current env weakens default-safe Action MCP policy."""
    issues: list[str] = []
    if env.get("TG_BLOCK_DIRECT_TELETHON_WRITE", "1") != "1":
        issues.append("TG_BLOCK_DIRECT_TELETHON_WRITE must be 1")
    if env.get("TG_ALLOW_DIRECT_TELETHON_WRITE", "0") == "1":
        issues.append("TG_ALLOW_DIRECT_TELETHON_WRITE must stay 0")
    if env.get("TG_ENFORCE_ACTION_PROCESS", "1") != "1":
        issues.append("TG_ENFORCE_ACTION_PROCESS must be 1")
    if not require_allowlist:
        issues.append("TG_ACTIONS_REQUIRE_ALLOWLIST must be 1")
    elif not parse_allowlist(env.get("TG_ACTIONS_ALLOWED_GROUPS", "")):
        issues.append("TG_ACTIONS_ALLOWED_GROUPS must not be empty when allowlist is required")
    if not require_confirmation_text:
        issues.append("TG_ACTIONS_REQUIRE_CONFIRMATION_TEXT must be 1")
    elif not env.get("TG_ACTIONS_CONFIRMATION_PHRASE", "").strip():
        issues.append("TG_ACTIONS_CONFIRMATION_PHRASE must be set locally")
    elif env.get("TG_ACTIONS_CONFIRMATION_PHRASE", "").strip().lower() in {
        "change-me-local-confirmation-phrase",
        "set-local-confirmation-phrase",
    }:
        issues.append("TG_ACTIONS_CONFIRMATION_PHRASE must not use the public placeholder")
    if not require_approval_code:
        issues.append("TG_ACTIONS_REQUIRE_APPROVAL_CODE must be 1")
    if not idempotency_enabled:
        issues.append("TG_ACTIONS_IDEMPOTENCY_ENABLED must be 1")
    return issues


def validate_confirmation_text(
    *,
    confirmation_text: str,
    dry_run: bool,
    require_confirmation_text: bool,
    min_confirmation_text_len: int,
    confirmation_phrase: str,
) -> tuple[bool, str | None]:
    """Validate explicit human confirmation phrase for non-dry-run writes."""
    if dry_run or not require_confirmation_text:
        return True, None

    text = (confirmation_text or "").strip()
    if len(text) < int(min_confirmation_text_len):
        return (
            False,
            "Execution blocked: add confirmation_text from user in this thread "
            f"(min {min_confirmation_text_len} chars).",
        )
    if confirmation_phrase and text.lower() != confirmation_phrase:
        return (
            False,
            f"Execution blocked: confirmation_text must be exactly '{confirmation_phrase}'.",
        )
    return True, None
