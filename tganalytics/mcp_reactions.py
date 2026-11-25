"""Small, guarded acknowledgement reactions and exact read-only message annotations."""

import hashlib

from telethon import utils

from telethon.tl.functions.messages import (
    GetAvailableReactionsRequest,
    SendReactionRequest,
)
from telethon.tl.types import ReactionEmoji


async def read_reactions(ns, group, message_id):
    manager = await ns["ctx"].get_manager()
    entity = await manager._resolve_read_entity(group, allow_users=True)
    message = await ns["safe_call"](
        ns["ctx"].client.get_messages, entity, ids=message_id, operation_type="api")
    if not message or getattr(message, "id", None) != message_id:
        raise ValueError("reaction_message_unavailable")
    reactions = getattr(message, "reactions", None)
    own = [getattr(r.reaction, "emoticon", "custom") for r in (reactions.results if reactions else [])
           if r.chosen_order is not None]
    return {"message_id": message_id, "own_reactions": own, "observed": True}


def register_reads(ns):
    @ns["mcp"].tool()
    async def tg_get_message_annotations(group: str, message_id: int) -> dict:
        """Read exact sender, quote and reply annotations without granting action authority."""
        if message_id <= 0:
            return {"error": "positive message_id required"}
        manager = await ns["ctx"].get_manager()
        entity = await manager._resolve_read_entity(group, allow_users=True)
        message = await ns["safe_call"](
            ns["ctx"].client.get_messages, entity, ids=message_id, operation_type="api")
        if not message or message.id != message_id:
            return {"observed": False, "message_id": message_id}
        body = message.message or ""
        first = len(body) - len(body.lstrip())
        leading_quote = any(
            type(e).__name__ in {"MessageEntityBlockquote", "MessageEntityPre", "MessageEntityCode"}
            and e.offset <= first < e.offset + e.length for e in (message.entities or []))
        header = message.reply_to
        reply_peer = getattr(header, "reply_to_peer_id", None)
        return {"observed": True, "message_id": message.id, "sender_id": message.sender_id,
                "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "leading_quote": leading_quote, "forwarded": bool(message.fwd_from or message.via_bot_id),
                "reply_to_msg_id": getattr(header, "reply_to_msg_id", None),
                "reply_peer_id": utils.get_peer_id(reply_peer) if reply_peer else None}

    @ns["mcp"].tool()
    async def tg_get_message_reactions(group: str, message_id: int) -> dict:
        """Read the logged-in account's reactions to one exact message."""
        if message_id <= 0:
            return {"error": "positive message_id required"}
        return await read_reactions(ns, group, message_id)

    @ns["mcp"].tool()
    async def tg_get_available_reactions() -> dict:
        """Read active standard emoji; no custom/paid reaction purchase."""
        await ns["ctx"].get_manager()
        result = await ns["safe_call"](
            ns["ctx"].client, GetAvailableReactionsRequest(hash=0), operation_type="api")
        return {"emojis": [r.reaction for r in result.reactions if not r.inactive]}


def register_actions(ns):
    @ns["mcp"].tool()
    async def tg_acknowledge_message(
        group: str, message_id: int, emoji: str,
        dry_run: bool = True, confirm: bool = False,
        confirmation_text: str = "", approval_code: str = "",
    ) -> dict:
        """Acknowledge one message with a standard reaction, preserving human reactions.

        All normal ActionMCP allowlist, process, approval and duplicate gates apply.
        A successful write still requires independent ReadMCP verification.
        """
        ok, error = ns["_check_action_preconditions"](
            group, dry_run=dry_run, confirm=confirm, confirmation_text=confirmation_text)
        if not ok:
            return ns["_blocked"](error or "preconditions failed")
        if message_id <= 0 or emoji not in {"🤖", "👾", "👀"}:
            return ns["_blocked"]("invalid acknowledgement message or emoji")
        action_hash = ns["_hash_payload"]({
            "action": "acknowledge_message", "target": ns["_normalize_target"](group),
            "message_id": message_id, "emoji": emoji})
        ok, error, meta = ns["_approval_gate"](
            action_hash=action_hash, dry_run=dry_run, approval_code=approval_code)
        if not ok:
            return ns["_blocked"](error or "approval gate blocked")
        if dry_run:
            return {"success": True, "dry_run": True, "action_hash": action_hash,
                    "target": group, "message_id": message_id, "emoji": emoji, **(meta or {})}
        duplicate, retry = ns["_check_recent_duplicate"](action_hash)
        if duplicate:
            return ns["_blocked"]("duplicate action blocked", retry_after_sec=retry)
        observed = await read_reactions(ns, group, message_id)
        if observed["own_reactions"]:
            return {"success": True, "written": False, "preserved_existing": True, **observed}
        manager = await ns["ctx"].get_manager()
        entity = await manager._resolve_target_entity(group)
        # Fence before RPC: an exception may mean accepted remotely, never retry blindly.
        ns["_mark_action_executed"](action_hash)
        try:
            await ns["safe_call"](ns["ctx"].client, SendReactionRequest(
                peer=entity, msg_id=message_id, reaction=[ReactionEmoji(emoticon=emoji)],
                big=False, add_to_recent=False), operation_type="group_msg")
        except Exception:
            return {"success": False, "reconciliation_required": True,
                    "message_id": message_id, "action_hash": action_hash}
        return {"success": True, "written": True, "message_id": message_id,
                "emoji": emoji, "action_hash": action_hash}
