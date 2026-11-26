"""Durable ordinary-send reservation; force never releases an unknown effect."""
import json
import secrets
import time
from mcp_rich_publication import ledger


def database(path):
    db=ledger({'IDEMPOTENCY_FILE':path})
    db.execute('CREATE TABLE IF NOT EXISTS ordinary_dispatches '
               '(hash TEXT PRIMARY KEY, nonce TEXT, state TEXT, created REAL, payload TEXT, receipt TEXT)')
    return db


def reserve(path, action_hash, payload, force=False, window=86400):
    db=database(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM ordinary_dispatches WHERE hash=?',(action_hash,)).fetchone()
            if row and row['state'] in ('pending','unknown'):
                return {'allowed':False,'error_code':'prior_send_effect_unknown',
                        'receipt':json.loads(row['receipt']) if row['receipt'] else None,
                        'automatic_retry_allowed':False}
            if row and row['state']=='sent' and not force and window>0 and time.time()-row['created']<window:
                return {'allowed':False,'error_code':'duplicate_send_reserved','automatic_retry_allowed':False}
            nonce=secrets.token_hex(16)
            db.execute("INSERT OR REPLACE INTO ordinary_dispatches VALUES (?,?, 'pending',?,?,NULL)",
                       (action_hash,nonce,time.time(),json.dumps(payload,ensure_ascii=False)))
        return {'allowed':True,'nonce':nonce}
    finally:db.close()


def finish(path, action_hash, nonce, state, receipt):
    if state not in ('sent','unknown','rejected'):raise ValueError('invalid_dispatch_state')
    db=database(path)
    try:
        with db:
            changed=db.execute('UPDATE ordinary_dispatches SET state=?,receipt=? '
                               'WHERE hash=? AND nonce=?',
                               (state,json.dumps(receipt,ensure_ascii=False),action_hash,nonce)).rowcount
            if changed!=1:raise ValueError('dispatch_reservation_changed')
    finally:db.close()


def operation(path, action_hash, operation_id=None):
    import re
    if not re.fullmatch('[a-f0-9]{64}', action_hash):
        raise ValueError('exact_action_hash_required')
    db = database(path)
    try:
        row = db.execute('SELECT * FROM ordinary_dispatches WHERE hash=?',(action_hash,)).fetchone()
        if row is None:
            raise ValueError('plain_operation_not_found')
        if operation_id is not None and row['nonce'] != operation_id:
            raise ValueError('plain_operation_revision_mismatch')
        return dict(row)
    finally:
        db.close()


async def inspect_or_reconcile(api, group, action_hash, operation_id=None, message_id=None):
    import hashlib
    from telethon import utils
    from tganalytics.domain.groups import _safe_api_call
    row = operation(api['IDEMPOTENCY_FILE'], action_hash, operation_id)
    payload = json.loads(row['payload'])
    if api['_hash_payload'](payload) != action_hash:
        raise ValueError('plain_frozen_payload_hash_mismatch')
    if api['_normalize_target'](group) != payload['target']:
        raise ValueError('plain_frozen_target_mismatch')
    identity = payload.get('identity')
    if not isinstance(identity, dict):
        return {'success':False,'error_code':'plain_identity_unbound_legacy_operation',
                'automatic_retry_allowed':False}
    manager = await api['ctx'].get_manager()
    me = await _safe_api_call(manager.client.get_me, max_retries=0)
    entity = await manager._resolve_target_entity(group)
    if me.id != identity['actor_id'] or utils.get_peer_id(entity) != identity['peer_id']:
        raise ValueError('plain_read_identity_target_mismatch')
    result = {'success':True,'action_hash':action_hash,'operation_id':row['nonce'],
              'ledger_state':row['state'],'ledger_unchanged':True,'automatic_retry_allowed':False,
              'identity':identity,'reply_to_msg_id':payload.get('reply_to_msg_id'),
              'text_sha256':hashlib.sha256(payload['text'].encode()).hexdigest(),
              'provider_receipt':json.loads(row['receipt']) if row['receipt'] else None}
    if message_id is None:
        return result
    if type(message_id) is not int or not 0 < message_id < 2**31:
        raise ValueError('exact_plain_message_id_required')
    message = await _safe_api_call(manager.client.get_messages,entity,ids=message_id,max_retries=0)
    reply = getattr(getattr(message,'reply_to',None),'reply_to_msg_id',None)
    actual_sender = getattr(message,'from_id',None)
    if actual_sender is None and getattr(message,'post',False):
        actual_sender = getattr(message,'peer_id',None)
    if actual_sender is None and identity['peer_id'] == me.id and getattr(message,'out',False):
        actual_sender = me.id
    peer = getattr(message,'peer_id',None)
    matches = (type(message).__name__ == 'Message' and message.id == message_id and
               peer is not None and utils.get_peer_id(peer) == identity['peer_id'] and
               actual_sender is not None and utils.get_peer_id(actual_sender) == identity['sender_id'] and
               (reply or None) == (payload.get('reply_to_msg_id') or None) and
               getattr(message,'message',None) == payload['text'] and
               getattr(message,'rich_message',None) is None and
               type(getattr(message,'media',None)).__name__ != 'MessageMediaUnsupported')
    receipt = result['provider_receipt'] or {}
    causal_match = type(receipt.get('message_id')) is int and receipt['message_id'] == message_id
    verified = bool(matches and causal_match)
    return {**result,'success':verified,'effect_status':'verified' if verified else 'unknown',
            'content_match':bool(matches),'operation_receipt_id_match':causal_match,
            'constraint':None if causal_match else 'plain_operation_causality_unproven',
            'message_id':message_id,'read_method':'get_messages_exact_id',
            'message_type':type(message).__name__,
            'no_match_is_not_proof_of_no_effect':True,
            'readback_sha256':hashlib.sha256((getattr(message,'message',None) or '').encode()).hexdigest()}
