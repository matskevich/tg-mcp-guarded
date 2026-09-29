"""Read-focused MCP server for tganalytics."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.environ.get("TG_ENV_FILE") or None)

# Read profile should never perform direct writes.
os.environ.setdefault("TG_BLOCK_DIRECT_TELETHON_WRITE", "1")
os.environ.setdefault("TG_ALLOW_DIRECT_TELETHON_WRITE", "0")
os.environ.setdefault("TG_ENFORCE_ACTION_PROCESS", "1")
os.environ.setdefault("TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS", "actions_mcp")
os.environ.setdefault("TG_WRITE_CONTEXT", "read_mcp")
os.environ.setdefault("TG_ACTION_PROCESS", "0")
os.environ.setdefault("TG_SESSION_RUNTIME_MODE", "copy")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp_server_common import MCPServerContext  # noqa: E402

from tganalytics.infra.limiter import get_rate_limiter, safe_call  # noqa: E402
from tganalytics.infra.metrics import snapshot  # noqa: E402

SERVER_NAME = os.environ.get("TG_MCP_SERVER_NAME", "tganalytics-read")
ALLOW_SESSION_SWITCH = os.environ.get("TG_ALLOW_SESSION_SWITCH", "1") == "1"

mcp = FastMCP(SERVER_NAME)
ctx = MCPServerContext(allow_session_switch=ALLOW_SESSION_SWITCH, server_profile="read")


@mcp.tool()
async def tg_list_sessions() -> dict:
    """List available Telegram sessions in data/sessions/."""
    return await ctx.list_sessions()


@mcp.tool()
async def tg_use_session(session_name: str) -> dict:
    """Switch to a different Telegram session (e.g. 'example_account')."""
    return await ctx.use_session(session_name)


@mcp.tool()
async def tg_get_group_info(group: str) -> dict:
    """Get info about a Telegram dialog target (group/channel/direct chat)."""
    manager = await ctx.get_manager()
    result = await manager.get_group_info(group)
    return result or {"error": "Group not found"}


@mcp.tool()
async def tg_get_participants(group: str, limit: int = 100) -> dict:
    """Get participants of a Telegram group (id, username, first_name, is_premium, ...)."""
    manager = await ctx.get_manager()
    participants = await manager.get_participants(group, limit=limit)
    return {"count": len(participants), "participants": participants}


@mcp.tool()
async def tg_search_participants(group: str, query: str, limit: int = 50) -> dict:
    """Search group participants by name or username."""
    manager = await ctx.get_manager()
    participants = await manager.search_participants(group, query, limit=limit)
    return {"count": len(participants), "participants": participants}


@mcp.tool()
async def tg_get_messages(
    group: str,
    limit: int = 100,
    min_id: int = 0,
    max_id: int = 0,
    offset_id: int = 0,
) -> dict:
    """Get messages from a Telegram dialog target (group/channel/direct chat)."""
    manager = await ctx.get_manager()
    messages = await manager.get_messages(
        group,
        limit=limit,
        min_id=min_id,
        max_id=max_id,
        offset_id=offset_id,
    )
    return {"count": len(messages), "messages": messages}


@mcp.tool()
async def tg_get_messages_since(group: str, cutoff_iso: str, limit: int = 0) -> dict:
    """Get messages from newest to oldest until cutoff_iso (YYYY-MM-DD or ISO datetime)."""
    manager = await ctx.get_manager()
    messages = await manager.get_messages_since(
        group, cutoff_iso=cutoff_iso, limit=limit
    )
    return {"count": len(messages), "messages": messages, "cutoff_iso": cutoff_iso}


@mcp.tool()
async def tg_search_messages(
    group: str,
    query: str,
    limit: int = 100,
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Search messages in a Telegram dialog; read-only."""
    manager = await ctx.get_manager()
    messages = await manager.search_messages(
        group,
        query=query,
        limit=limit,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    return {
        "count": len(messages),
        "messages": messages,
        "query": query,
        "date_from": date_from or None,
        "date_to": date_to or None,
    }


@mcp.tool()
async def tg_search_global_messages(
    query: str,
    limit: int = 100,
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Search messages across Telegram dialogs; read-only."""
    manager = await ctx.get_manager()
    messages = await manager.search_global_messages(
        query=query,
        limit=limit,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    return {
        "count": len(messages),
        "messages": messages,
        "query": query,
        "date_from": date_from or None,
        "date_to": date_to or None,
    }


@mcp.tool()
async def tg_get_message_count(group: str) -> dict:
    """Get total number of messages in a Telegram dialog target."""
    manager = await ctx.get_manager()
    count = await manager.get_message_count(group)
    if count is not None:
        return {"group": group, "message_count": count}
    return {"group": group, "error": "Could not retrieve message count"}


@mcp.tool()
async def tg_get_group_creation_date(group: str) -> dict:
    """Get approximate creation date of a Telegram dialog target (via first message)."""
    manager = await ctx.get_manager()
    dt = await manager.get_group_creation_date(group)
    if dt is not None:
        return {"group": group, "creation_date": dt.isoformat()}
    return {"group": group, "error": "Could not determine creation date"}


@mcp.tool()
async def tg_get_my_dialogs(limit: int = 100, dialog_type: str = "all") -> dict:
    """List groups, channels and chats the current account is a member of."""
    manager = await ctx.get_manager()
    dialogs = await manager.get_my_dialogs(limit=limit, dialog_type=dialog_type)
    return {"count": len(dialogs), "dialogs": dialogs}


@mcp.tool()
async def tg_resolve_username(username: str) -> dict:
    """Resolve a Telegram @username to user/channel/chat info (id, type, name)."""
    manager = await ctx.get_manager()
    result = await manager.resolve_username(username)
    return result or {"error": f"Could not resolve username '{username}'"}


@mcp.tool()
async def tg_get_user_by_id(user_id: int) -> dict:
    """Get user info by numeric Telegram ID."""
    await ctx.get_manager()
    try:
        entity = await safe_call(ctx.client.get_entity, user_id, operation_type="api")
        return {
            "id": entity.id,
            "username": getattr(entity, "username", None),
            "first_name": getattr(entity, "first_name", None),
            "last_name": getattr(entity, "last_name", None),
            "phone": getattr(entity, "phone", None),
            "is_bot": getattr(entity, "bot", False),
            "is_premium": getattr(entity, "premium", False),
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def tg_search_contacts(query: str, limit: int = 50) -> dict:
    """Search Telegram contacts/global contacts by query without mutating contacts."""
    await ctx.get_manager()
    clean_query = str(query or "").strip()
    if not clean_query:
        return {"error": "query is empty"}
    clean_limit = max(1, min(int(limit or 50), 200))

    try:
        from telethon.tl.functions.contacts import SearchRequest

        result = await safe_call(
            ctx.client,
            SearchRequest(q=clean_query, limit=clean_limit),
            operation_type="api",
        )
        users = {getattr(user, "id", None): user for user in (getattr(result, "users", None) or [])}
        chats = {getattr(chat, "id", None): chat for chat in (getattr(result, "chats", None) or [])}

        def peer_id(peer):
            return (
                getattr(peer, "user_id", None)
                or getattr(peer, "chat_id", None)
                or getattr(peer, "channel_id", None)
            )

        def peer_type(peer) -> str:
            name = type(peer).__name__.lower()
            if "user" in name:
                return "user"
            if "channel" in name:
                return "channel"
            if "chat" in name:
                return "chat"
            return name

        def serialize_peer(peer, bucket: str) -> dict:
            pid = peer_id(peer)
            ptype = peer_type(peer)
            entity = users.get(pid) if ptype == "user" else chats.get(pid)
            return {
                "id": pid,
                "type": ptype,
                "bucket": bucket,
                "title": (
                    " ".join(
                        str(part or "").strip()
                        for part in [
                            getattr(entity, "first_name", None),
                            getattr(entity, "last_name", None),
                        ]
                        if str(part or "").strip()
                    )
                    if ptype == "user"
                    else getattr(entity, "title", None)
                ),
                "username": getattr(entity, "username", None),
                "phone": getattr(entity, "phone", None) if ptype == "user" else None,
                "is_bot": getattr(entity, "bot", False) if ptype == "user" else False,
            }

        entries = []
        for bucket in ("my_results", "results"):
            for peer in getattr(result, bucket, None) or []:
                entries.append(serialize_peer(peer, bucket))
        return {"query": clean_query, "count": len(entries), "results": entries}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def tg_get_contacts(limit: int = 5000) -> dict:
    """List Telegram account contacts with visible phone fields; read-only."""
    await ctx.get_manager()
    clean_limit = max(1, min(int(limit or 5000), 10000))
    try:
        from telethon.tl.functions.contacts import GetContactsRequest

        result = await safe_call(
            ctx.client,
            GetContactsRequest(hash=0),
            operation_type="api",
        )
        contacts = []
        for user in (getattr(result, "users", None) or [])[:clean_limit]:
            contacts.append(
                {
                    "id": getattr(user, "id", None),
                    "title": " ".join(
                        str(part or "").strip()
                        for part in [
                            getattr(user, "first_name", None),
                            getattr(user, "last_name", None),
                        ]
                        if str(part or "").strip()
                    )
                    or getattr(user, "username", None),
                    "username": getattr(user, "username", None),
                    "phone": getattr(user, "phone", None),
                    "is_bot": getattr(user, "bot", False),
                    "is_premium": getattr(user, "premium", False),
                }
            )
        return {"count": len(contacts), "contacts": contacts}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def tg_get_saved_contacts(limit: int = 5000) -> dict:
    """List saved/imported phone contacts; read-only."""
    await ctx.get_manager()
    clean_limit = max(1, min(int(limit or 5000), 10000))
    try:
        from telethon.tl.functions.contacts import GetSavedRequest

        result = await safe_call(
            ctx.client,
            GetSavedRequest(),
            operation_type="api",
        )
        raw_contacts = result if isinstance(result, list) else (
            getattr(result, "contacts", None)
            or getattr(result, "saved", None)
            or []
        )
        contacts = []
        for item in raw_contacts[:clean_limit]:
            date = getattr(item, "date", None)
            contacts.append(
                {
                    "phone": getattr(item, "phone", None),
                    "first_name": getattr(item, "first_name", None),
                    "last_name": getattr(item, "last_name", None),
                    "date": date.isoformat() if hasattr(date, "isoformat") else date,
                    "type": type(item).__name__,
                }
            )
        return {"count": len(contacts), "contacts": contacts}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def tg_download_media(
    group: str, message_id: int, output_dir: str = "data/downloads"
) -> dict:
    """Download a file/media from a Telegram message to a local directory."""
    manager = await ctx.get_manager()
    path = await manager.download_media(group, message_id, output_dir)
    if path:
        return {"success": True, "path": path}
    return {"success": False, "error": "Download failed or message has no media"}


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
