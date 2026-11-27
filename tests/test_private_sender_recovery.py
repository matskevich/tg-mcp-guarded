import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types
from telethon.errors import SendAsPeerInvalidError
import mcp_server_actions as actions
import mcp_plain_guard as guard
from tganalytics.domain.groups import GroupManager
import tganalytics.domain.groups as groups


@pytest.mark.asyncio
@pytest.mark.parametrize('target', [20, 10, -1000000000020])
async def test_personal_private_rpc_omits_sender_but_channel_keeps_binding(monkeypatch, target):
    peer = types.PeerUser(target) if target > 0 else types.PeerChannel(20)
    sent = types.Message(id=99,peer_id=peer,from_id=types.PeerUser(10),
        date=datetime.now(timezone.utc),message='synthetic')
    client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=10)),
        get_input_entity=AsyncMock(return_value=types.InputPeerSelf()),send_message=AsyncMock(return_value=sent))
    manager=GroupManager(client);manager._resolve_target_entity=AsyncMock(return_value=peer)
    manager._is_recent_conversation=AsyncMock(return_value=False)
    async def once(func,*args,**kwargs):
        assert kwargs.pop('max_retries',0)==0
        kwargs.pop('operation_type',None);kwargs.pop('operation_target_id',None)
        return await func(*args,**kwargs)
    monkeypatch.setattr(groups,'_safe_api_call',once)
    identity,_=await manager.prepare_send_identity(str(target),10,10,10)
    out=await manager.send_message(str(target),'synthetic',return_receipt=True,
        send_as_id=10,expected_actor_id=10,expected_sender_id=10)
    assert out['message_id']==99 and identity=={'actor_id':10,'sender_id':10,'send_as_id':10,'peer_id':target}
    assert ('send_as' in client.send_message.call_args.kwargs)==(target<0)
    client.send_message.side_effect=SendAsPeerInvalidError(request=None)
    out=await manager.send_message(str(target),'synthetic',return_receipt=True,
        send_as_id=10,expected_actor_id=10,expected_sender_id=10)
    assert out['state']=='rejected' and out['definitive_rpc_rejection'] and out['effect_status']=='not_performed'
    client.send_message.side_effect=TimeoutError()
    out=await manager.send_message(str(target),'synthetic',return_receipt=True,
        send_as_id=10,expected_actor_id=10,expected_sender_id=10)
    assert out['state']=='unknown' and not out.get('definitive_rpc_rejection')


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type',['SendAsPeerInvalidError','TimeoutError','RPCError'])
async def test_saved_exact_rejection_only_changes_bound_nonce(monkeypatch,tmp_path,error_type):
    path=tmp_path/'idem.json';payload={'action':'send_message','target':'20','text':'synthetic',
        'identity':{'actor_id':10,'sender_id':10,'send_as_id':10,'peer_id':20}}
    h=actions._hash_payload(payload);reservation=guard.reserve(path,h,payload);nonce=reservation['nonce']
    guard.finish(path,h,nonce,'unknown',{'submitted':True,'state':'unknown','error_type':error_type,'message_id':None})
    manager=SimpleNamespace(client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=10))),
        _resolve_target_entity=AsyncMock(return_value=types.PeerUser(20)))
    api={'IDEMPOTENCY_FILE':path,'_hash_payload':actions._hash_payload,'_normalize_target':actions._normalize_target,
        'ctx':SimpleNamespace(get_manager=AsyncMock(return_value=manager)),
        '_check_action_preconditions':lambda *a,**k:(True,None),'_blocked':lambda x:{'success':False},
        '_approval_gate':lambda **k:(True,None,{'approval_code':'synthetic'}),'CONFIRMATION_PHRASE':'synthetic'}
    before=guard.operation(path,h)
    with pytest.raises(ValueError,match='revision_mismatch'):
        await guard.reconcile_rejection(api,'20',h,'wrong')
    preview=await guard.reconcile_rejection(api,'20',h,nonce)
    assert guard.operation(path,h)==before
    assert not guard.reserve(path,h,payload,force=True)['allowed']
    out=await guard.reconcile_rejection(api,'20',h,nonce,False,True,'synthetic','synthetic')
    if error_type!='SendAsPeerInvalidError':
        assert not out['success'] and guard.operation(path,h)==before
        assert not guard.proven_rejected(path,h)
    else:
        assert preview['dry_run'] and out['effect_status']=='not_performed'
        assert out['reconciled'] and out['telegram_rpc_invoked'] is False
        after=guard.operation(path,h)
        assert after['nonce']==nonce and after['payload']==before['payload'] and after['hash']==h
        assert guard.proven_rejected(path,h)
        repeated=await guard.reconcile_rejection(api,'20',h,nonce)
        assert repeated['ledger_unchanged'] and repeated['reconciled']
        assert guard.reserve(path,h,payload)['allowed']
        # A different/new dispatch nonce can never inherit the old rejection.
        assert not guard.proven_rejected(path,h)


@pytest.mark.asyncio
async def test_channel_display_sender_is_not_omitted(monkeypatch):
    sender=-1000000000040
    client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=10)),
        get_input_entity=AsyncMock(return_value=types.InputPeerChannel(40,0)),
        send_message=AsyncMock(return_value=types.Message(id=99,peer_id=types.PeerChannel(20),
            from_id=types.PeerChannel(40),date=datetime.now(timezone.utc),message='synthetic')))
    manager=GroupManager(client);manager._resolve_target_entity=AsyncMock(return_value=types.PeerChannel(20))
    manager._is_recent_conversation=AsyncMock(return_value=False)
    async def once(func,*args,**kwargs):
        kwargs.pop('max_retries',None);kwargs.pop('operation_type',None);kwargs.pop('operation_target_id',None)
        return await func(*args,**kwargs)
    monkeypatch.setattr(groups,'_safe_api_call',once)
    result=await manager.send_message('-1000000000020','synthetic',return_receipt=True,
        send_as_id=sender,expected_actor_id=10,expected_sender_id=sender)
    assert result['sender_id']==sender
    assert isinstance(client.send_message.call_args.kwargs['send_as'],types.InputPeerChannel)


@pytest.mark.asyncio
async def test_private_null_short_ack_verified_only_by_exact_causal_read(tmp_path):
    path=tmp_path/'idem.json';payload={'action':'send_message','target':'20','text':'synthetic',
        'identity':{'actor_id':10,'sender_id':10,'send_as_id':10,'peer_id':20}}
    h=actions._hash_payload(payload);op=guard.reserve(path,h,payload)
    guard.finish(path,h,op['nonce'],'sent',{'submitted':True,'message_id':99,'sender_id':None})
    message=types.Message(id=99,peer_id=types.PeerUser(20),out=True,message='synthetic',date=datetime.now(timezone.utc))
    client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=10)),get_messages=AsyncMock(return_value=message))
    manager=SimpleNamespace(client=client,_resolve_target_entity=AsyncMock(return_value=types.PeerUser(20)))
    api={'IDEMPOTENCY_FILE':path,'_hash_payload':actions._hash_payload,'_normalize_target':actions._normalize_target,
         'ctx':SimpleNamespace(get_manager=AsyncMock(return_value=manager))}
    out=await guard.inspect_or_reconcile(api,'20',h,op['nonce'],99)
    assert out['success'] and out['operation_receipt_id_match'] and out['readback_sender_id']==10
    assert out['provider_receipt']['sender_id'] is None and out['ledger_unchanged']
    message.out=False
    assert not (await guard.inspect_or_reconcile(api,'20',h,op['nonce'],99))['success']
    message.out=True
    assert not (await guard.inspect_or_reconcile(api,'20',h,op['nonce'],100))['success']
