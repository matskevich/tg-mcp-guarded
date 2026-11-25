"""Owner-only shared catalog adapter for the existing guarded ActionMCP.

The existing credential runtime injects its usual profile. A child env scopes
only this invocation's exact creation title AFTER injection. No secret cache,
Telegram session, direct client or automatic action approval belongs here.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from tg_action_bridge import MCPStdioClient, ServerSpec  # noqa: E402

mcp = FastMCP("private-telegram-channel")


def invoke_native(name: str, arguments: dict) -> dict:
    if name not in {"tg_create_private_channel", "tg_auth_status"}:
        raise ValueError("unsupported native operation")
    title = arguments.get("title", "").strip()
    if name == "tg_create_private_channel" and not 0 < len(title) <= 128:
        return {"success": False, "error": "Exact channel title required"}
    # The profile and actor are operator-selected, never caller-supplied paths.
    command = ["run", "--env-file=" + str(Path.home() / ".config/agents/tgmcp-actions.env.tpl"),
               "--", "env", "TG_SESSION_RUNTIME_MODE=copy",
               "TG_SESSION_RUNTIME_PROFILE=channel_catalog"]
    if title:
        command.append("TG_ACTIONS_ALLOWED_GROUPS=" + title)
    command.extend([str(ROOT / "venv/bin/python3"),
                    str(ROOT / "tganalytics/mcp_server_actions.py")])
    spec = ServerSpec("tgmcp_actions", str(Path.home() / ".local/bin/op-agents"),
                      command, {}, str(ROOT))
    with MCPStdioClient(spec, dict(os.environ)) as client:
        auth = client.call_tool("tg_auth_status", {})
        if not auth.get("authorized") or auth.get("account", {}).get("username") != "dmatskevich":
            return {"success": False, "error": "Pinned owner account not authorized"}
        if name == "tg_auth_status":
            return {"authorized": True, "account": auth["account"]}
        if auth["account"]["id"] != arguments["expected_actor_id"]:
            return {"success": False, "error": "Actor account ID mismatch"}
        policy = client.call_tool("tg_get_actions_policy", {})
        guards = ("actions_enabled", "require_allowlist", "require_confirmation_text",
                  "require_approval_code", "idempotency_enabled", "enforce_action_process",
                  "direct_telethon_write_guard")
        if not all(policy.get(k) is True for k in guards) or policy.get("unsafe_override"):
            return {"success": False, "error": "Required native action guards unavailable"}
        if title.lower() not in policy.get("allowed_targets", []):
            return {"success": False, "error": "Exact process-scoped title unavailable"}
        return client.call_tool(name, arguments)


@mcp.tool(annotations={"readOnlyHint": True})
async def tg_private_channel_status() -> dict:
    """Verify the pinned owner account before a channel creation preview. Read-only."""
    return await asyncio.to_thread(invoke_native, "tg_auth_status", {})


@mcp.tool(annotations={"readOnlyHint": False})
async def tg_create_private_channel(title: str, expected_actor_id: int,
        dry_run: bool = True, confirm: bool = False,
        confirmation_text: str = "", approval_code: str = "") -> dict:
    """Create an empty private broadcast channel with unsigned posts on owner request.

    Preview with dry_run=true, then preserve exact title/account and pass the
    returned approval_code and confirmation phrase with dry_run=false, confirm=true.
    Members and publisher grants are separate guarded Telegram actions. Unknown
    creation effects require independent readback; never create a replacement.
    This catalog route scopes only the exact requested title in its child process.
    """
    return await asyncio.to_thread(invoke_native, "tg_create_private_channel", dict(
        title=title, expected_actor_id=expected_actor_id, dry_run=dry_run,
        confirm=confirm, confirmation_text=confirmation_text, approval_code=approval_code))


if __name__ == "__main__":
    mcp.run()
