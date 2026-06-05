#!/usr/bin/env python3
"""Render tg-mcp config snippet for read-only or full profile."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_read_server(
    repo: Path,
    server_name: str,
    session_name: str,
    actions_session_name: str,
    expected_username: str = "",
) -> dict:
    read_session_path = str((repo / f"data/sessions/{session_name}.session").resolve())
    actions_session_path = str((repo / f"data/sessions/{actions_session_name}.session").resolve())
    env = {
        "PYTHONPATH": f"{(repo / 'tganalytics').resolve()}:{repo.resolve()}",
        "TG_SESSIONS_DIR": str((repo / "data/sessions").resolve()),
        "TG_SESSION_PATH": read_session_path,
        "TG_READ_SESSION_PATH": read_session_path,
        "TG_ACTIONS_SESSION_PATH": actions_session_path,
        "TG_SESSION_PATH_CONFLICT_MODE": "warn",
        "TG_SESSION_CONFLICT_REGISTRY_FILE": str(
            (repo / "data/anti_spam/session_registry.json").resolve()
        ),
        "TG_ALLOW_SESSION_SWITCH": "0",
        "TG_BLOCK_DIRECT_TELETHON_WRITE": "1",
        "TG_ALLOW_DIRECT_TELETHON_WRITE": "0",
        "TG_ENFORCE_ACTION_PROCESS": "1",
        "TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS": "actions_mcp",
        "TG_WRITE_CONTEXT": "read_mcp",
        "TG_ACTION_PROCESS": "0",
        "TG_SESSION_RUNTIME_MODE": "copy",
        "TG_RECEIVE_UPDATES": "0",
        "TG_SESSION_LOCK_MODE": "shared",
        "TG_GLOBAL_RPS_MODE": "shared",
        "TG_FLOOD_CIRCUIT_THRESHOLD_SEC": "300",
        "TG_FLOOD_CIRCUIT_COOLDOWN_SEC": "900",
    }
    if expected_username:
        env["TG_EXPECTED_USERNAME"] = expected_username

    return {
        server_name: {
            "command": str((repo / "venv/bin/python3").resolve()),
            "args": [str((repo / "tganalytics/mcp_server_read.py").resolve())],
            "env": env,
        }
    }


def _build_actions_server(
    repo: Path,
    server_name: str,
    session_name: str,
    read_session_name: str,
    expected_username: str = "",
) -> dict:
    actions_session_path = str((repo / f"data/sessions/{session_name}.session").resolve())
    read_session_path = str((repo / f"data/sessions/{read_session_name}.session").resolve())
    env = {
        "PYTHONPATH": f"{(repo / 'tganalytics').resolve()}:{repo.resolve()}",
        "TG_SESSIONS_DIR": str((repo / "data/sessions").resolve()),
        "TG_SESSION_PATH": actions_session_path,
        "TG_READ_SESSION_PATH": read_session_path,
        "TG_ACTIONS_SESSION_PATH": actions_session_path,
        "TG_SESSION_PATH_CONFLICT_MODE": "warn",
        "TG_SESSION_CONFLICT_REGISTRY_FILE": str(
            (repo / "data/anti_spam/session_registry.json").resolve()
        ),
        "TG_ALLOW_SESSION_SWITCH": "0",
        "TG_ACTIONS_ENABLED": "1",
        "TG_ACTIONS_REQUIRE_ALLOWLIST": "1",
        "TG_ACTIONS_ALLOWED_GROUPS": "",
        "TG_ACTIONS_MAX_MESSAGE_LEN": "2000",
        "TG_ACTIONS_MAX_FILE_MB": "20",
        "TG_ACTIONS_REQUIRE_CONFIRMATION_TEXT": "1",
        "TG_ACTIONS_CONFIRMATION_PHRASE": "",
        "TG_ACTIONS_MIN_CONFIRM_TEXT_LEN": "6",
        "TG_ACTIONS_REQUIRE_APPROVAL_CODE": "1",
        "TG_ACTIONS_APPROVAL_TTL_SEC": "1800",
        "TG_ACTIONS_APPROVAL_MIN_AGE_SEC": "30",
        "TG_ACTIONS_APPROVAL_FILE": str((repo / "data/anti_spam/action_approvals.json").resolve()),
        "TG_ACTIONS_IDEMPOTENCY_ENABLED": "1",
        "TG_ACTIONS_IDEMPOTENCY_WINDOW_SEC": "86400",
        "TG_ACTIONS_IDEMPOTENCY_FILE": str(
            (repo / "data/anti_spam/action_idempotency.json").resolve()
        ),
        "TG_ACTIONS_BATCH_FILE": str((repo / "data/anti_spam/action_batches.json").resolve()),
        "TG_ACTIONS_BATCH_TTL_HOURS": "168",
        "TG_ACTIONS_BATCH_APPROVAL_LEASE_SEC": "86400",
        "TG_ACTIONS_BATCH_RUN_LEASE_SEC": "1800",
        "TG_ACTIONS_UNSAFE_OVERRIDE": "0",
        "TG_BLOCK_DIRECT_TELETHON_WRITE": "1",
        "TG_ALLOW_DIRECT_TELETHON_WRITE": "0",
        "TG_ENFORCE_ACTION_PROCESS": "1",
        "TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS": "actions_mcp",
        "TG_WRITE_CONTEXT": "actions_mcp",
        "TG_ACTION_PROCESS": "1",
        "TG_RECEIVE_UPDATES": "0",
        "TG_SESSION_LOCK_MODE": "shared",
        "TG_GLOBAL_RPS_MODE": "shared",
        "TG_FLOOD_CIRCUIT_THRESHOLD_SEC": "300",
        "TG_FLOOD_CIRCUIT_COOLDOWN_SEC": "900",
        "MAX_GROUP_MSGS_PER_DAY": "30",
    }
    if expected_username:
        env["TG_EXPECTED_USERNAME"] = expected_username

    return {
        server_name: {
            "command": str((repo / "venv/bin/python3").resolve()),
            "args": [str((repo / "tganalytics/mcp_server_actions.py").resolve())],
            "env": env,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Absolute path to tg-mcp repository")
    parser.add_argument(
        "--profile",
        choices=("read", "full"),
        default="read",
        help="read: only tgmcp-read, full: read + actions",
    )
    parser.add_argument("--read-server-name", default="tgmcp-read")
    parser.add_argument("--actions-server-name", default="tgmcp-actions")
    parser.add_argument("--read-session-name", default="read_only_ro")
    parser.add_argument("--actions-session-name", default="actions_session")
    parser.add_argument(
        "--expected-username",
        default="",
        help="Optional expected @username for session fail-fast checks (example: example_account)",
    )
    parser.add_argument("--output", help="Write result to file; default stdout")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    servers = {}
    read_session_path = str((repo / f"data/sessions/{args.read_session_name}.session").resolve())
    actions_session_path = str(
        (repo / f"data/sessions/{args.actions_session_name}.session").resolve()
    )
    if args.profile == "full" and read_session_path == actions_session_path:
        print(
            "[tg-mcp][session-warning] read/actions resolve to the same session file. "
            "This is supported but not recommended for routine concurrent MCP traffic. "
            "Prefer read -> *_ro.session and actions -> write session.",
            file=sys.stderr,
        )
    servers.update(
        _build_read_server(
            repo,
            args.read_server_name,
            args.read_session_name,
            args.actions_session_name,
            expected_username=args.expected_username,
        )
    )
    if args.profile == "full":
        servers.update(
            _build_actions_server(
                repo,
                args.actions_server_name,
                args.actions_session_name,
                args.read_session_name,
                expected_username=args.expected_username,
            )
        )

    payload = {"mcpServers": servers}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
