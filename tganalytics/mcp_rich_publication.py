"""Guarded rich publishing over the provider's existing ActionMCP policy.

The durable operation reservation survives the ordinary duplicate window.
An unknown dispatch is reconciled; it is never force-retried here.
"""
import json
import os
import re
import sqlite3

from telethon.errors import SendAsPeerInvalidError

from telethon import utils
from tganalytics.domain.groups import _safe_api_call
from tganalytics.infra.limiter import safe_call
from tganalytics.infra.rich_messages import (
    UnsupportedRichContent, canonical_rich_tree, digest, discussion_root, full_message, prepare_rich, transport,
)


def ledger(api):
    path = api["IDEMPOTENCY_FILE"].with_name("rich_publication.sqlite")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, hash TEXT NOT NULL, "
               "payload TEXT NOT NULL, state TEXT NOT NULL, message INTEGER)")
    if "before_id" not in {row[1] for row in db.execute("PRAGMA table_info(operations)")}:
        db.execute("ALTER TABLE operations ADD COLUMN before_id INTEGER")
        db.commit()
    if "receipt" not in {row[1] for row in db.execute("PRAGMA table_info(operations)")}:
        db.execute("ALTER TABLE operations ADD COLUMN receipt TEXT")
        db.commit()
    return db


def acknowledgement(result, target, random_id):
    """Retain exact effect evidence, not unrelated returned users/contacts."""
    updates = []
    for item in getattr(result, "updates", []):
        name = type(item).__name__
        if name == "UpdateMessageID" and item.random_id == random_id:
            updates.append(transport(item))
        elif name in {"UpdateNewMessage", "UpdateNewChannelMessage"}:
            if utils.get_peer_id(item.message.peer_id) == target:
                updates.append(transport(item))
    return {"response_type":type(result).__name__, "updates":updates,
            "update_types":[type(item).__name__ for item in getattr(result,"updates",[])],
            "short_sent":transport(result) if type(result).__name__ == "UpdateShortSentMessage" else None}


def unaccepted_reply_targets():
    raw = os.environ.get('TG_RICH_REPLY_UNACCEPTED_TARGETS','')
    if not raw:
        return []
    parts = raw.split(',')
    if any(not re.fullmatch('-[1-9][0-9]{0,18}', part) for part in parts):
        raise ValueError('invalid_rich_reply_consumer_profile')
    return sorted(set(map(int,parts)))


def normalized(value):
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()
                if item is not None and item is not False}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


async def send_rich(api, *, group, rich_message, operation_id, expected_actor_id,
                    expected_sender_id, send_as_id, reply_to_msg_id=0,
                    discussion_channel="", channel_post_id=0,
                    dry_run=True, confirm=False, confirmation_text="", approval_code=""):
    allowed, error = api["_check_action_preconditions"](
        group, dry_run=dry_run, confirm=confirm, confirmation_text=confirmation_text)
    if not allowed:
        return api["_blocked"](error or "preconditions failed")
    if not dry_run and os.getenv("TG_RICH_PREVIEW_ONLY", "0") == "1":
        return {"success": False, "error_code": "rich_actor_preview_only",
                "submitted": False, "dry_run": False}
    if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", operation_id) or
            type(expected_actor_id) is not int or expected_actor_id <= 0 or
            type(send_as_id) is not int or send_as_id == 0 or
            send_as_id != expected_sender_id or type(reply_to_msg_id) is not int or reply_to_msg_id < 0):
        return api["_blocked"]("exact operation, actor, displayed sender and reply identity required")
    try:
        rich, preview = prepare_rich(rich_message)
    except (UnsupportedRichContent, ValueError) as error:
        return {"success": False, "error_code": "unsupported_rich_payload", "error": str(error)}
    manager = await api["ctx"].get_manager()
    me = await _safe_api_call(manager.client.get_me)
    if me.id != expected_actor_id:
        return api["_blocked"]("actor account mismatch")
    entity = await manager._resolve_read_entity(group, allow_users=True)
    if entity is None:
        return api["_blocked"]("exact target not resolved")
    target = utils.get_peer_id(entity)
    if str(target) != group:
        return api["_blocked"]("exact numeric target required")
    if reply_to_msg_id and target in unaccepted_reply_targets():
        return {'success':False,'submitted':False,'error_code':'rich_reply_consumer_not_accepted',
                'consumer_acceptance':'known_rendering_failure','native_rpc_not_invoked':True,
                'compatible_native_tool':'tg_send_message','compatible_facade_tool':'tg_send_plain_message',
                'wire_format_required':'ordinary_plain_text','automatic_fallback':False}
    if bool(discussion_channel) != bool(channel_post_id):
        return api["_blocked"]("discussion channel and post must be supplied together")
    root = None
    if discussion_channel:
        root = await discussion_root(manager, discussion_channel, channel_post_id)
        if (not root.get("success") or root["discussion_id"] != target or
                root["discussion_root_id"] != reply_to_msg_id):
            return api["_blocked"]("linked discussion target/root mismatch")
    sender = await _safe_api_call(manager.client.get_input_entity, send_as_id)
    sender_id = me.id if type(sender).__name__ == "InputPeerSelf" else utils.get_peer_id(sender)
    if sender_id != expected_sender_id:
        return api["_blocked"]("send-as identity mismatch")
    if reply_to_msg_id:
        reply = await _safe_api_call(manager.client.get_messages, entity, ids=reply_to_msg_id)
        if (reply is None or getattr(reply, "id", None) != reply_to_msg_id or
                utils.get_peer_id(reply.peer_id) != target):
            return api["_blocked"]("reply message identity not verified in exact target")
    payload = {"action": "send_rich_message", "target": target,
               "actor": expected_actor_id, "sender": expected_sender_id,
               "reply_to_msg_id": reply_to_msg_id, "discussion_channel": discussion_channel,
               "channel_post_id": channel_post_id, "operation_id": operation_id,
               "rich_message": preview["rich_message"]}
    action_hash = api["_hash_payload"](payload)
    db = ledger(api)
    try:
        existing = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if existing:
            if existing["hash"] != action_hash:
                return api["_blocked"]("operation id already bound to another exact payload")
            if existing["state"] != "rejected":
                return {"success": existing["state"] == "verified", "state": existing["state"],
                        "message_id": existing["message"], "action_hash": action_hash,
                        "submitted": True, "reconciliation_required": existing["state"] != "verified",
                        "new_dispatch": False}
            # Only a proven provider rejection may be attempted again, with a
            # new preview and one-time approval. Unknown reservations stay fenced.
        ok, error, meta = api["_approval_gate"](
            action_hash=action_hash, dry_run=dry_run, approval_code=approval_code)
        if not ok:
            return api["_blocked"](error or "approval blocked")
        if dry_run:
            return {"success": True, "dry_run": True, "target": target,
                    "actor_id": expected_actor_id, "send_as_id": expected_sender_id,
                    "reply_to_msg_id": reply_to_msg_id,
                    "discussion": {k: root[k] for k in ("channel_id", "channel_post_id",
                        "discussion_id", "discussion_root_id")} if root else None,
                    "preview": preview, "action_hash": action_hash,
                    "confirmation_text_required": api["CONFIRMATION_PHRASE"], **(meta or {})}
        from telethon.tl.functions.messages import SendMessageRequest
        from telethon.tl.types import InputReplyToMessage
        random_id = int(action_hash[:15], 16) or 1
        request = SendMessageRequest(peer=await manager.client.get_input_entity(entity),
            message="", rich_message=rich, random_id=random_id,
            # Saved Messages has no alternate displayed identity; Telegram
            # rejects an explicit send_as there. Keep actor/sender in the exact
            # approval and independent readback, omit only this redundant flag.
            send_as=None if target == me.id == expected_sender_id else sender,
            reply_to=InputReplyToMessage(reply_to_msg_id=reply_to_msg_id) if reply_to_msg_id else None)
        head = await _safe_api_call(manager.client.get_messages, entity, limit=1)
        before_id = head[0].id if head else 0
        # Reserve before dispatch, under the same durable uniqueness constraint.
        try:
            with db:
                if existing and existing["state"] == "rejected":
                    changed = db.execute("UPDATE operations SET state='reserved',before_id=? "
                                         "WHERE id=? AND hash=? AND state='rejected' AND message IS NULL",
                                         (before_id, operation_id, action_hash)).rowcount
                    if changed != 1:
                        raise sqlite3.IntegrityError("rejection already claimed")
                else:
                    db.execute("INSERT INTO operations(id,hash,payload,state,message,before_id) "
                               "VALUES (?,?,?,'reserved',NULL,?)",
                               (operation_id, action_hash, json.dumps(payload, ensure_ascii=False), before_id))
        except sqlite3.IntegrityError:
            return {"success": False, "error_code": "concurrent_operation_reserved",
                    "reconciliation_required": True, "new_dispatch": False}
        api["_mark_action_executed"](action_hash)
        try:
            result = await safe_call(manager.client, request, max_retries=0,
                operation_type="dm" if target > 0 else "group_msg", operation_target_id=abs(target))
        except SendAsPeerInvalidError as error:
            # This exact RPC error rejects the sender before message creation.
            # Do not generalize timeouts/unknown RPC errors into a safe retry.
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS rejection_receipts "
                           "(operation_id TEXT, action_hash TEXT, error_type TEXT, evidence TEXT)")
                db.execute("INSERT INTO rejection_receipts VALUES (?,?,?,?)",
                           (operation_id, action_hash, type(error).__name__, "native_rpc_rejection"))
                db.execute("UPDATE operations SET state='rejected' WHERE id=?", (operation_id,))
            return {"success": False, "submitted": True, "state": "rejected",
                    "action_hash": action_hash, "error_type": type(error).__name__,
                    "effect_verified": False, "effect_status": "not_performed",
                    "reconciliation_required": False, "fresh_preview_required": True,
                    "automatic_retry_allowed": False}
        except Exception as error:
            return {"success": False, "submitted": True, "state": "unknown",
                    "action_hash": action_hash, "error_type": type(error).__name__,
                    "reconciliation_required": True, "automatic_retry_allowed": False}
        ids = [item.id for item in getattr(result, "updates", [])
               if type(item).__name__ == "UpdateMessageID" and item.random_id == random_id]
        if not ids:
            ids = [item.message.id for item in getattr(result, "updates", [])
                   if type(item).__name__ in {"UpdateNewMessage", "UpdateNewChannelMessage"}
                   and utils.get_peer_id(item.message.peer_id) == target]
        message_id = ids[0] if len(set(ids)) == 1 else None
        if type(result).__name__ == "UpdateShortSentMessage":
            message_id = result.id
        with db:
            db.execute("UPDATE operations SET state='sent',message=?,receipt=? WHERE id=?",
                       (message_id, json.dumps(acknowledgement(result,target,random_id),ensure_ascii=False), operation_id))
        return {"success": True, "submitted": True, "message_id": message_id,
                "target": target, "state": "sent", "action_hash": action_hash,
                "effect_verified": False, "reconciliation_required": True}
    finally:
        db.close()


async def reconcile_rich(api, group, operation_id, message_id):
    allowed, error = api["_check_target_allowed"](group)
    if not allowed:
        return api["_blocked"](error or "target blocked")
    db = ledger(api)
    try:
        row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not row:
            return {"success": False, "error_code": "unknown_operation"}
        payload = json.loads(row["payload"])
        if payload.get("action") != "send_rich_message":
            return api["_blocked"]("operation kind mismatch")
        if str(payload["target"]) != group:
            return api["_blocked"]("operation target mismatch")
        if row["message"] is not None and row["message"] != message_id:
            return api["_blocked"]("known message identity mismatch")
        if row["before_id"] is None or message_id <= row["before_id"]:
            return api["_blocked"]("message is not newer than this operation's dispatch watermark")
        manager = await api["ctx"].get_manager()
        me = await _safe_api_call(manager.client.get_me)
        if me.id != payload["actor"]:
            return api["_blocked"]("operation actor account mismatch")
        result = await full_message(manager, group, message_id, thread_root_id=payload["reply_to_msg_id"],
                                    discussion_channel=payload["discussion_channel"],
                                    channel_post_id=payload["channel_post_id"])
        raw = result.get("raw_message", {})
        rich = raw.get("rich_message") or {}
        sender = raw.get("from_id") or raw.get("peer_id")
        sender_id = (sender or {}).get("user_id")
        if sender and "channel_id" in sender:
            sender_id = -1000000000000 - sender["channel_id"]
        reply = (raw.get("reply_to") or {}).get("reply_to_msg_id", 0)
        expected = payload["rich_message"]
        matches = (result.get("content_complete") is True and sender_id == payload["sender"]
                   and reply == payload["reply_to_msg_id"] and
                   canonical_rich_tree(normalized(rich.get("blocks"))) == canonical_rich_tree(normalized(expected.get("blocks"))) and
                   bool(rich.get("rtl")) == bool(expected.get("rtl")))
        for name in ("photos", "documents"):
            matches = matches and ([item["id"] for item in rich.get(name, [])] ==
                                   [item["id"] for item in expected.get(name, [])])
        if not matches:
            return {"success": False, "state": "unknown", "readback_mismatch": True,
                    "automatic_retry_allowed": False,
                    "read_error_code": result.get("error_code"),
                    "read_evidence": result.get("read_evidence"),
                    "retrieval": result.get("retrieval"),
                    "provider_receipt_available": bool(row["receipt"])}
        with db:
            db.execute("UPDATE operations SET state='verified',message=? WHERE id=?", (message_id, operation_id))
        return {"success": True, "state": "verified", "message_id": message_id,
                "target": payload["target"], "action_hash": row["hash"],
                "rich_sha256": result.get("rich_sha256"), "independent_readback": True,
                "exact_rich_tree_match": normalized(rich.get("blocks")) == normalized(expected.get("blocks")),
                "normalization_policy": "implicit_auto_url_and_adjacent_plain_concat_only",
                "retrieval": result.get("retrieval")}
    finally:
        db.close()
