"""Exact channel publishing grants, using the existing MCP policy and client guard."""
from __future__ import annotations

import hashlib
import json
from telethon import utils
from telethon.tl.functions.channels import GetParticipantRequest, EditAdminRequest
from telethon.tl.types import Channel, User, ChatAdminRights
from tganalytics.domain.groups import _safe_api_call


def rights_dict(rights):
    if rights is None:
        return {}
    return {k: bool(v) for k, v in rights.to_dict().items() if k != "_"}


def role_digest(role):
    return hashlib.sha256(json.dumps(role, sort_keys=True).encode()).hexdigest()


async def inspect_member(ctx, group, user, channel_id, user_id, actor_id):
    if (type(channel_id) is not int or channel_id >= -1000000000000
            or type(user_id) is not int or user_id <= 0
            or type(actor_id) is not int or actor_id <= 0):
        raise ValueError("Exact channel, member and actor IDs required")
    manager = await ctx.get_manager()
    client = manager.client
    me = await _safe_api_call(client.get_me)
    if me.id != actor_id:
        raise ValueError("Actor account ID mismatch")
    channel = await _safe_api_call(client.get_entity, int(group) if group.lstrip('-').isdigit() else group)
    member = await _safe_api_call(client.get_entity, int(user) if user.isdigit() else user)
    if (not isinstance(channel, Channel) or not channel.broadcast or channel.megagroup
            or utils.get_peer_id(channel) != channel_id):
        raise ValueError("Exact broadcast channel mismatch")
    if not isinstance(member, User) or member.id != user_id or member.bot:
        raise ValueError("Exact human member mismatch")
    result = await _safe_api_call(client, GetParticipantRequest(channel, member))
    participant = result.participant
    if getattr(participant, "user_id", None) != user_id:
        raise ValueError("Participant readback identity mismatch")
    role = {"channel_id": channel_id, "user_id": user_id, "actor_id": actor_id,
            "participant_type": type(participant).__name__,
            "admin_rights": rights_dict(getattr(participant, "admin_rights", None)),
            "rank": getattr(participant, "rank", None)}
    return client, channel, member, {"success": True, **role, "state_hash": role_digest(role)}


def minimal_role(role, enabled, accept_platform_ban_users=False):
    # 'other' is Telegram's implied management flag; it grants no named capability.
    rights = role["admin_rights"]
    allowed = {"post_messages", "other"}
    if accept_platform_ban_users:
        allowed.add("ban_users")
    extras = [key for key, value in rights.items() if value and key not in allowed]
    if extras or role["participant_type"] not in {"ChannelParticipant", "ChannelParticipantSelf", "ChannelParticipantAdmin"}:
        raise ValueError("Existing role is outside minimal publisher scope; no implicit revocation")
    return bool(rights.get("post_messages")) == enabled


def register_read(mcp, ctx):
    @mcp.tool()
    async def tg_get_channel_member_rights(group: str, user: str, expected_channel_id: int,
                                          expected_user_id: int, expected_actor_id: int) -> dict:
        """Read exact channel member admin flags. No access hashes, phones or writes."""
        try:
            _, _, _, role = await inspect_member(ctx, group, user, expected_channel_id,
                                                  expected_user_id, expected_actor_id)
            return role
        except Exception as exc:
            return {"success": False, "error": str(exc), "role_unknown": True}
    return tg_get_channel_member_rights


def register_actions(api):
    mcp = api["mcp"]

    @mcp.tool()
    async def tg_set_channel_publisher(group: str, user: str, expected_channel_id: int,
            expected_user_id: int, expected_actor_id: int, expected_state_hash: str,
            enabled: bool = True, accept_platform_ban_users: bool = False,
            rollback_of_state_hash: str = "",
            dry_run: bool = True, confirm: bool = False,
            confirmation_text: str = "", approval_code: str = "") -> dict:
        """Grant/revoke only post_messages on an exact broadcast channel.

        Refuses existing broader roles, stale previews and forced unknown retries.
        Uses existing allowlist, process, approval and idempotency guards.
        accept_platform_ban_users requires separate explicit owner authority for
        the observed extra effective right; it is bound into the approval hash.
        """
        if str(expected_channel_id) != group or len(expected_state_hash) != 64:
            return api["_blocked"]("Exact numeric allowlisted channel and role state hash required")
        allowed, error = api["_check_action_preconditions"](
            group, dry_run=dry_run, confirm=confirm, confirmation_text=confirmation_text)
        if not allowed:
            return api["_blocked"](error or "preconditions failed")
        payload = {"action": "set_channel_publisher", "target": group, "user": user,
                   "channel_id": expected_channel_id, "user_id": expected_user_id,
                   "actor_id": expected_actor_id, "state_hash": expected_state_hash,
                   "enabled": enabled}
        if accept_platform_ban_users:
            payload["accept_platform_ban_users"] = True
        if rollback_of_state_hash:
            payload["rollback_of_state_hash"] = rollback_of_state_hash
        action_hash = api["_hash_payload"](payload)
        ok, error, meta = api["_approval_gate"](action_hash=action_hash,
                                               dry_run=dry_run, approval_code=approval_code)
        if not ok:
            return api["_blocked"](error or "approval blocked")
        if not dry_run:
            duplicate, retry = api["_check_recent_duplicate"](action_hash)
            if duplicate:
                return {"success": False, "duplicate_blocked": True,
                        "retry_after_sec": retry, "reconciliation_required": True}
        submitted = False
        after = None
        try:
            client, channel, member, before = await inspect_member(api["ctx"], group, user,
                expected_channel_id, expected_user_id, expected_actor_id)
            if before["state_hash"] != expected_state_hash:
                raise ValueError("Role changed since read; refresh preview")
            if rollback_of_state_hash:
                # Only undo this exact actor/target's recent local grant from a
                # known ordinary-member state, not an unrelated administrator.
                original = {k:v for k,v in payload.items() if k != "rollback_of_state_hash"}
                original.update(state_hash=rollback_of_state_hash, enabled=True)
                minted, _ = api["_check_recent_duplicate"](api["_hash_payload"](original))
                extras = {k for k,v in before["admin_rights"].items() if v} - {"post_messages", "ban_users", "other"}
                if enabled or not minted or extras:
                    raise ValueError("Rollback must match this target's recorded grant")
                no_op = not before["admin_rights"]
            else:
                no_op = minimal_role(before, enabled, accept_platform_ban_users)
            actor = await _safe_api_call(client, GetParticipantRequest(channel, "me"))
            actor_rights = getattr(actor.participant, "admin_rights", None)
            if (type(actor.participant).__name__ != "ChannelParticipantCreator"
                    and not getattr(actor_rights, "add_admins", False)):
                raise ValueError("Actor cannot manage channel administrators")
            result = {"success": True, "dry_run": dry_run, "before": before,
                      "enabled": enabled, "no_op": no_op, "action_hash": action_hash,
                      "confirmation_text_required": api["CONFIRMATION_PHRASE"] if api["REQUIRE_CONFIRMATION_TEXT"] else None}
            if dry_run:
                result.update(meta or {})
                return result
            if no_op:
                return {**result, "after": before, "read_back_verified": True}
            # Reserve before the RPC. A transport failure can have committed remotely.
            api["_mark_action_executed"](action_hash)
            submitted = True
            await _safe_api_call(client, EditAdminRequest(channel, member,
                ChatAdminRights(post_messages=True) if enabled else ChatAdminRights(), ""))
            _, _, _, after = await inspect_member(api["ctx"], group, user,
                expected_channel_id, expected_user_id, expected_actor_id)
            verified = minimal_role(after, enabled, accept_platform_ban_users)
            return {**result, "success": verified, "submitted": True, "after": after,
                    "read_back_verified": verified, "reconciliation_required": not verified}
        except Exception as exc:
            return {"success": False, "dry_run": dry_run, "submitted": submitted,
                    "error": str(exc), "action_hash": action_hash,
                    "reconciliation_required": submitted, "observed_after": after}
    return tg_set_channel_publisher
