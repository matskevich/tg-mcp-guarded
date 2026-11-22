from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import mcp_server_actions as actions
from tganalytics.domain import groups
from tganalytics.infra import tele_client

@pytest.fixture
def gated(monkeypatch,tmp_path):
    for k,v in dict(SAFE_STARTUP_BLOCK_REASON=None,ACTIONS_ENABLED=True,REQUIRE_ALLOWLIST=True,
        ALLOWED_TARGETS={'-100123','source'},REQUIRE_CONFIRMATION_TEXT=True,
        CONFIRMATION_PHRASE='confirm-test',REQUIRE_APPROVAL_CODE=True,APPROVAL_MIN_AGE_SEC=0,
        IDEMPOTENCY_ENABLED=True,APPROVAL_FILE=tmp_path/'a.json',IDEMPOTENCY_FILE=tmp_path/'i.json').items():
        monkeypatch.setattr(actions,k,v)
    async def forward(*args,**kwargs):return {'success':True,'dry_run':kwargs['dry_run']}
    manager=SimpleNamespace(forward_messages=AsyncMock(side_effect=forward))
    monkeypatch.setattr(actions,'ctx',SimpleNamespace(get_manager=AsyncMock(return_value=manager)))
    return manager

@pytest.mark.asyncio
async def test_topic_is_in_exact_approval_and_dedupe(gated):
    args=dict(from_group='source',to_group='-100123',message_ids=[7],to_topic_id=186)
    p=await actions.tg_forward_messages(**args)
    assert p['dry_run']
    bad=await actions.tg_forward_messages(**{**args,'to_topic_id':187},dry_run=False,confirm=True,
        confirmation_text='confirm-test',approval_code=p['approval_code'])
    assert not bad['success'] and gated.forward_messages.await_count==1
    ok=await actions.tg_forward_messages(**args,dry_run=False,confirm=True,
        confirmation_text='confirm-test',approval_code=p['approval_code'])
    assert ok['success'] and not ok['dry_run']
    assert gated.forward_messages.call_args.kwargs['to_topic_id']==186
    p=await actions.tg_forward_messages(**args)
    again=await actions.tg_forward_messages(**args,dry_run=False,confirm=True,
        confirmation_text='confirm-test',approval_code=p['approval_code'])
    assert again['duplicate_blocked']

@pytest.mark.asyncio
async def test_invalid_topic_and_unauthorized_source_fail(gated):
    for topic in [0,-1,True,'186']:
        assert not (await actions.tg_forward_messages(from_group='source',to_group='-100123',message_ids=[7],to_topic_id=topic))['success']
    assert not (await actions.tg_forward_messages(from_group='other',to_group='-100123',message_ids=[7],to_topic_id=186))['success']
    gated.forward_messages.assert_not_awaited()

@pytest.mark.asyncio
async def test_topic_forward_rpc_preserves_author_and_dryrun(monkeypatch):
    m=object.__new__(groups.GroupManager);m.client=object()
    m._resolve_target_entity=AsyncMock(return_value=SimpleNamespace(forum=True))
    async def call(client,request,**kwargs):
        if type(request).__name__=='GetForumTopicsByIDRequest':return SimpleNamespace(topics=[SimpleNamespace(id=186,closed=False)])
        return SimpleNamespace(updates=[])
    safe=AsyncMock(side_effect=call);monkeypatch.setattr(groups,'_safe_api_call',safe)
    p=await m.forward_messages('source','-100123',[7],to_topic_id=186,dry_run=True)
    assert p['success'] and safe.await_count==1
    ok=await m.forward_messages('source','-100123',[7],to_topic_id=186)
    assert ok['success']
    req=safe.call_args.args[1]
    assert type(req).__name__=='ForwardMessagesRequest' and req.top_msg_id==186
    assert req.id==[7] and not req.drop_author
    assert tele_client._is_telethon_write_request(req)
    m._resolve_target_entity=AsyncMock(return_value=SimpleNamespace(forum=False))
    assert not (await m.forward_messages('source','-100123',[7],to_topic_id=186))['success']
