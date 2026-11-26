"""Guarded in-place rich edits with optimistic before-version checks.

Telegram has no atomic server CAS for this RPC. We check again immediately
before dispatch, durably fence unknown effects, and reconcile a fresh read.
"""
import json
import re
import sqlite3
import os

from telethon import utils
from telethon.tl.functions.messages import EditMessageRequest
from tganalytics.domain.groups import _safe_api_call
from tganalytics.infra.limiter import safe_call
from tganalytics.infra.rich_messages import (
    UnsupportedRichContent, canonical_rich_tree, full_message, prepare_rich, transport,
)
from mcp_rich_publication import ledger, normalized, unaccepted_reply_targets


def invariants(raw):
    # Replies/reactions/views are live counters; they cannot be used as edit CAS.
    return {key: raw.get(key) for key in (
        'id', 'peer_id', 'from_id', 'reply_to', 'media', 'grouped_id', 'post',
        'reply_markup', 'fwd_from', 'via_bot_id', 'post_author')}


def sender_id(raw):
    peer = raw.get('from_id') or raw.get('peer_id') or {}
    if 'channel_id' in peer:
        return -1000000000000 - peer['channel_id']
    return peer.get('user_id')


def matches_rich(result, expected):
    rich = (result.get('raw_message') or {}).get('rich_message') or {}
    return (result.get('success') is True and result.get('content_complete') is True
            and canonical_rich_tree(normalized(rich.get('blocks'))) ==
                canonical_rich_tree(normalized(expected.get('blocks')))
            and bool(rich.get('rtl')) == bool(expected.get('rtl'))
            and all([item['id'] for item in rich.get(name, [])] ==
                    [item['id'] for item in expected.get(name, [])]
                    for name in ('photos', 'documents')))


async def edit_rich(api, *, group, message_id, rich_message, operation_id,
                    expected_actor_id, expected_sender_id, expected_rich_sha256,
                    dry_run=True, confirm=False, confirmation_text='', approval_code=''):
    allowed, error = api['_check_action_preconditions'](
        group, dry_run=dry_run, confirm=confirm, confirmation_text=confirmation_text)
    if not allowed:
        return api['_blocked'](error or 'preconditions failed')
    if not dry_run and os.getenv('TG_RICH_PREVIEW_ONLY', '0') == '1':
        return dict(success=False, error_code='rich_actor_preview_only', submitted=False)
    if (not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', operation_id)
            or type(message_id) is not int or message_id <= 0
            or type(expected_actor_id) is not int or expected_actor_id <= 0
            or type(expected_sender_id) is not int or expected_sender_id == 0
            or not re.fullmatch('[a-f0-9]{64}', expected_rich_sha256)):
        return api['_blocked']('exact operation/message/actor/sender/before hash required')
    try:
        rich, preview = prepare_rich(rich_message)
    except (UnsupportedRichContent, ValueError) as error:
        return dict(success=False, error_code='unsupported_rich_payload', error=str(error))
    manager = await api['ctx'].get_manager()
    me = await _safe_api_call(manager.client.get_me)
    if me.id != expected_actor_id:
        return api['_blocked']('actor account mismatch')
    entity = await manager._resolve_read_entity(group, allow_users=True)
    if entity is None or str(utils.get_peer_id(entity)) != group:
        return api['_blocked']('exact numeric target required')
    target = utils.get_peer_id(entity)
    request_payload = dict(action='edit_rich_message', target=target, message_id=message_id,
        actor=expected_actor_id, sender=expected_sender_id, before_hash=expected_rich_sha256,
        operation_id=operation_id, rich_message=preview['rich_message'])
    request_hash = api['_hash_payload'](request_payload)
    db = ledger(api)
    try:
        existing = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if existing:
            frozen = json.loads(existing['payload'])
            if frozen.get('request_hash') != request_hash:
                return api['_blocked']('operation id already bound to another exact payload')
            return dict(success=existing['state'] == 'verified', state=existing['state'],
                submitted=True, message_id=existing['message'], action_hash=existing['hash'],
                new_dispatch=False, reconciliation_required=existing['state'] != 'verified')
        before = await full_message(manager, group, message_id)
        raw = before.get('raw_message') or {}
        if (before.get('success') is not True or before.get('content_complete') is not True
                or before.get('format') != 'rich' or raw.get('id') != message_id
                or sender_id(raw) != expected_sender_id):
            return api['_blocked']('complete exact rich message and sender required')
        if target in unaccepted_reply_targets() and (raw.get('reply_to') or {}).get('reply_to_msg_id'):
            return dict(success=False, submitted=False,
                        error_code='rich_reply_consumer_not_accepted', native_rpc_not_invoked=True)
        if before.get('rich_sha256') != expected_rich_sha256:
            return dict(success=False, submitted=False, error_code='before_version_mismatch',
                        observed_rich_sha256=before.get('rich_sha256'))
        frozen = dict(request_payload, request_hash=request_hash, invariants=invariants(raw))
        action_hash = api['_hash_payload'](frozen)
        ok, error, meta = api['_approval_gate'](
            action_hash=action_hash, dry_run=dry_run, approval_code=approval_code)
        if not ok:
            return api['_blocked'](error or 'approval blocked')
        if dry_run:
            return dict(success=True, dry_run=True, target=target, message_id=message_id,
                actor_id=expected_actor_id, sender_id=expected_sender_id,
                before_rich_sha256=expected_rich_sha256, preview=preview,
                action_hash=action_hash, cas_policy='optimistic_double_read_not_server_atomic',
                confirmation_text_required=api['CONFIRMATION_PHRASE'], **(meta or {}))
        peer = await _safe_api_call(manager.client.get_input_entity, entity)
        # Recheck after approval and peer resolution; no mutable body is silently rebased.
        latest = await full_message(manager, group, message_id)
        if (latest.get('success') is not True or latest.get('content_complete') is not True
                or latest.get('rich_sha256') != expected_rich_sha256
                or invariants(latest.get('raw_message') or {}) != frozen['invariants']):
            return dict(success=False, submitted=False, error_code='before_version_changed_before_dispatch')
        request = EditMessageRequest(peer=peer, id=message_id, rich_message=rich)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                active = db.execute("SELECT payload FROM operations WHERE state IN ('reserved','unknown','sent')").fetchall()
                for row in active:
                    other = json.loads(row['payload'])
                    if (other.get('action') == 'edit_rich_message' and other.get('target') == target
                            and other.get('message_id') == message_id):
                        return dict(success=False, error_code='message_has_unreconciled_edit',
                                    new_dispatch=False, reconciliation_required=True)
                db.execute("INSERT INTO operations(id,hash,payload,state,message) VALUES (?,?,?,'reserved',?)",
                    (operation_id, action_hash, json.dumps(frozen, ensure_ascii=False), message_id))
        except sqlite3.IntegrityError:
            return dict(success=False, error_code='concurrent_operation_reserved',
                        new_dispatch=False, reconciliation_required=True)
        api['_mark_action_executed'](action_hash)
        try:
            result = await safe_call(manager.client, request, max_retries=0,
                operation_type='dm' if target > 0 else 'group_msg', operation_target_id=abs(target))
        except Exception as error:
            with db:
                db.execute("UPDATE operations SET state='unknown' WHERE id=?", (operation_id,))
            return dict(success=False, submitted=True, state='unknown', action_hash=action_hash,
                message_id=message_id, error_type=type(error).__name__,
                reconciliation_required=True, automatic_retry_allowed=False)
        updates = [transport(item) for item in getattr(result, 'updates', [])
            if type(item).__name__ in {'UpdateEditMessage', 'UpdateEditChannelMessage'}
            and item.message.id == message_id and utils.get_peer_id(item.message.peer_id) == target]
        with db:
            db.execute("UPDATE operations SET state='sent',receipt=? WHERE id=?",
                (json.dumps(dict(response_type=type(result).__name__, updates=updates), ensure_ascii=False), operation_id))
        return dict(success=True, submitted=True, state='sent', target=target, message_id=message_id,
                    action_hash=action_hash, effect_verified=False, reconciliation_required=True)
    finally:
        db.close()


async def reconcile_rich_edit(api, group, operation_id, message_id):
    allowed, error = api['_check_target_allowed'](group)
    if not allowed:
        return api['_blocked'](error or 'target blocked')
    db = ledger(api)
    try:
        row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if not row:
            return dict(success=False, error_code='unknown_operation')
        frozen = json.loads(row['payload'])
        if (frozen.get('action') != 'edit_rich_message' or str(frozen['target']) != group
                or frozen['message_id'] != message_id or row['message'] != message_id):
            return api['_blocked']('operation kind/target/message mismatch')
        manager = await api['ctx'].get_manager()
        me = await _safe_api_call(manager.client.get_me)
        if me.id != frozen['actor']:
            return api['_blocked']('operation actor account mismatch')
        result = await full_message(manager, group, message_id)
        if (not matches_rich(result, frozen['rich_message']) or
                invariants(result.get('raw_message') or {}) != frozen['invariants']):
            return dict(success=False, state='unknown', readback_mismatch=True,
                        automatic_retry_allowed=False, reconciliation_required=True)
        with db:
            db.execute("UPDATE operations SET state='verified' WHERE id=?", (operation_id,))
        return dict(success=True, state='verified', message_id=message_id, target=frozen['target'],
            action_hash=row['hash'], rich_sha256=result.get('rich_sha256'),
            independent_readback=True, preserved_message_metadata=True,
            normalization_policy='implicit_auto_url_and_adjacent_plain_concat_only')
    finally:
        db.close()
