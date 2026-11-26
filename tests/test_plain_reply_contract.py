from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from telethon.tl import types
import mcp_server_actions as actions
from tganalytics.domain.groups import GroupManager
import tganalytics.domain.groups as groups
from mcp_plain_guard import reserve, finish


@pytest.mark.asyncio
async def test_plain_preview_binds_actor_sender_reply_and_returns_actual_id(monkeypatch,tmp_path):
    client=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=10)),
        get_input_entity=AsyncMock(return_value=types.InputPeerSelf()),
        get_messages=AsyncMock(return_value=types.Message(id=98,peer_id=types.PeerChannel(20),
            date=datetime.now(timezone.utc),message='root')),
        send_message=AsyncMock(return_value=types.Message(id=99,peer_id=types.PeerChannel(20),
            from_id=types.PeerUser(10),date=datetime.now(timezone.utc),message='synthetic\n',
            reply_to=types.MessageReplyHeader(reply_to_msg_id=98))))
    manager=GroupManager(client)
    manager._resolve_target_entity=AsyncMock(return_value=types.PeerChannel(20))
    manager._is_recent_conversation=AsyncMock(return_value=False)
    async def once(func,*args,**kwargs):
        assert kwargs.pop('max_retries',0)==0
        kwargs.pop('operation_type',None);kwargs.pop('operation_target_id',None)
        return await func(*args,**kwargs)
    monkeypatch.setattr(groups,'_safe_api_call',once)
    monkeypatch.setattr(actions,'_check_action_preconditions',lambda *a,**k:(True,None))
    monkeypatch.setattr(actions,'_approval_gate',lambda **k:(True,None,{'approval_code':'synthetic-code'}))
    monkeypatch.setattr(actions,'_check_recent_duplicate',lambda *a:(False,0))
    monkeypatch.setattr(actions,'_mark_action_executed',lambda *a:None)
    monkeypatch.setattr(actions,'IDEMPOTENCY_FILE',tmp_path/'idempotency.json')
    monkeypatch.setattr(actions.ctx,'get_manager',AsyncMock(return_value=manager))
    args=dict(group='-1000000000020',message_text='synthetic\n',reply_to_msg_id=98,
              send_as_id=10,expected_actor_id=10,expected_sender_id=10)
    preview=await actions.tg_send_message(**args,dry_run=True)
    assert preview['message_text']=='synthetic\n'
    assert preview['reply_target']=={'peer_id':-1000000000020,'message_id':98}
    assert preview['identity']['sender_id']==10
    client.send_message.assert_not_awaited()
    sent=await actions.tg_send_message(**args,dry_run=False,confirm=True,approval_code='synthetic-code')
    assert sent['success'] and sent['message_id']==99 and sent['reply_to_msg_id']==98
    call=client.send_message.call_args
    assert call.kwargs['parse_mode'] is None and call.kwargs['reply_to']==98
    assert isinstance(call.kwargs['send_as'],types.InputPeerSelf)
    client.get_messages.return_value.peer_id=types.PeerChannel(21)
    denied=await actions.tg_send_message(**args,dry_run=True)
    assert denied['success'] is False
    assert client.send_message.await_count==1


def test_unknown_ordinary_effect_survives_restart_and_force_does_not_release_it(tmp_path):
    path=tmp_path/'idempotency.json'
    first=reserve(path,'hash',{'target':'synthetic'},window=0)
    assert first['allowed']
    assert not reserve(path,'hash',{},force=True,window=0)['allowed']
    finish(path,'hash',first['nonce'],'unknown',{'message_id':99,'submitted':True})
    again=reserve(path,'hash',{},force=True,window=0)
    assert again['error_code']=='prior_send_effect_unknown'
    assert again['receipt']['message_id']==99
