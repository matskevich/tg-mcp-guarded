"""Private broadcast creation through the existing ActionMCP policy gates."""
from telethon import utils
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.types import Channel


def register_actions(api):
    @api["mcp"].tool()
    async def tg_create_private_channel(title: str, expected_actor_id: int,
            dry_run: bool = True, confirm: bool = False,
            confirmation_text: str = "", approval_code: str = "") -> dict:
        """Create an empty private broadcast channel with unsigned posts.

        Allowlist the exact title for creation. Members and publishing rights
        remain separate guarded actions. Unknown creation effects block retries.
        """
        title = title.strip()
        if not 0 < len(title) <= 128 or expected_actor_id <= 0:
            return api["_blocked"]("Exact title and positive actor ID required")
        allowed, error = api["_check_action_preconditions"](
            title, dry_run=dry_run, confirm=confirm, confirmation_text=confirmation_text)
        if not allowed:
            return api["_blocked"](error or "preconditions failed")
        action_hash = api["_hash_payload"]({"action": "create_private_channel",
            "target": title, "actor_id": expected_actor_id,
            "broadcast": True, "signatures": False})
        ok, error, meta = api["_approval_gate"](action_hash=action_hash,
            dry_run=dry_run, approval_code=approval_code)
        if not ok:
            return api["_blocked"](error or "approval blocked")
        duplicate, retry = api["_check_recent_duplicate"](action_hash)
        if duplicate:
            return {"success": False, "duplicate_blocked": True,
                    "retry_after_sec": retry, "reconciliation_required": True}
        submitted = False
        channel_id = None
        try:
            manager = await api["ctx"].get_manager()
            me = await api["safe_call"](manager.client.get_me)
            if me.id != expected_actor_id:
                raise ValueError("Actor account ID mismatch")
            result = {"success": True, "dry_run": dry_run, "title": title,
                "actor_id": me.id, "broadcast": True, "private": True,
                "signatures": False, "action_hash": action_hash,
                "confirmation_text_required": api["CONFIRMATION_PHRASE"]}
            if dry_run:
                return {**result, **(meta or {})}
            api["_mark_action_executed"](action_hash)
            submitted = True
            updates = await api["safe_call"](manager.client,
                CreateChannelRequest(title=title, about="", broadcast=True,
                                     megagroup=False),
                operation_type="join", max_retries=0)
            matches = [c for c in updates.chats
                       if isinstance(c, Channel) and c.title == title]
            if len(matches) != 1:
                raise ValueError("Creation returned no unique matching channel")
            channel_id = utils.get_peer_id(matches[0])
            fresh = await api["safe_call"](manager.client.get_entity, matches[0])
            verified = (fresh.creator and fresh.broadcast and not fresh.megagroup
                and not fresh.username and not getattr(fresh, "usernames", None)
                and not fresh.signatures
                and not getattr(fresh, "signature_profiles", False)
                and utils.get_peer_id(fresh) == channel_id)
            return {**result, "success": bool(verified), "submitted": True,
                    "channel_id": channel_id, "read_back_verified": bool(verified),
                    "reconciliation_required": not verified}
        except Exception as exc:
            return {"success": False, "dry_run": dry_run, "submitted": submitted,
                    "channel_id": channel_id, "error": str(exc),
                    "reconciliation_required": submitted, "action_hash": action_hash}
    return tg_create_private_channel
