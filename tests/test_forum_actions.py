"""Forum mutation uses the same action gates as other writes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import mcp_server_actions as actions
from tganalytics.infra import tele_client

@pytest.fixture
def forum(monkeypatch, tmp_path):
    for k,v in dict(SAFE_STARTUP_BLOCK_REASON=None,ACTIONS_ENABLED=True,REQUIRE_ALLOWLIST=True,
        ALLOWED_TARGETS={'-100123'},REQUIRE_CONFIRMATION_TEXT=True,CONFIRMATION_PHRASE='test-confirm',
        REQUIRE_APPROVAL_CODE=True,APPROVAL_MIN_AGE_SEC=0,IDEMPOTENCY_ENABLED=True,
        APPROVAL_FILE=tmp_path/'a.json',IDEMPOTENCY_FILE=tmp_path/'i.json').items():
        monkeypatch.setattr(actions,k,v)
    entity=SimpleNamespace(forum=True,creator=True)
    manager=SimpleNamespace(_resolve_target_entity=AsyncMock(return_value=entity))
    monkeypatch.setattr(actions,'ctx',SimpleNamespace(get_manager=AsyncMock(return_value=manager),client=AsyncMock()))
    safe=AsyncMock(return_value=SimpleNamespace(updates=[]))
    monkeypatch.setattr(actions,'safe_call',safe)
    return entity,safe

@pytest.mark.asyncio
async def test_forum_exact_approval_and_uncertain_dedupe(forum):
    _,safe=forum
    args=dict(group='-100123',title='test topic')
    preview=await actions.tg_create_forum_topic(**args)
    assert preview['success'] and preview['dry_run']
    safe.assert_not_awaited()
    bad=await actions.tg_create_forum_topic(**{**args,'title':'changed'},dry_run=False,confirm=True,
        confirmation_text='test-confirm',approval_code=preview['approval_code'])
    assert not bad['success']
    result=await actions.tg_create_forum_topic(**args,dry_run=False,confirm=True,
        confirmation_text='test-confirm',approval_code=preview['approval_code'])
    assert result['reconciliation_required'] and not result['success']
    again=await actions.tg_create_forum_topic(**args)
    result=await actions.tg_create_forum_topic(**args,dry_run=False,confirm=True,
        confirmation_text='test-confirm',approval_code=again['approval_code'])
    assert result['duplicate_blocked'] and safe.await_count==1

@pytest.mark.asyncio
async def test_forum_rejects_other_target_or_nonforum(forum):
    entity,safe=forum
    assert not (await actions.tg_create_forum_topic(group='-100999',title='x'))['success']
    entity.forum=False
    assert not (await actions.tg_create_forum_topic(group='-100123',title='x'))['success']
    safe.assert_not_awaited()

def test_forum_rpc_is_write_guarded():
    from telethon.tl.functions.channels import CreateForumTopicRequest
    assert tele_client._is_telethon_write_request(CreateForumTopicRequest(channel=None,title='x'))
