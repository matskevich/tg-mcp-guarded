from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import mcp_server_actions as actions

@pytest.mark.asyncio
async def test_unpin_exact_plan_approval_and_duplicate_guard(monkeypatch,tmp_path):
    for key,value in dict(SAFE_STARTUP_BLOCK_REASON=None,ACTIONS_ENABLED=True,REQUIRE_ALLOWLIST=True,
            ALLOWED_TARGETS={'5139858'},REQUIRE_CONFIRMATION_TEXT=True,CONFIRMATION_PHRASE='exact-confirm',
            REQUIRE_APPROVAL_CODE=True,IDEMPOTENCY_ENABLED=True,APPROVAL_MIN_AGE_SEC=0,
            APPROVAL_FILE=tmp_path/'approvals.json',IDEMPOTENCY_FILE=tmp_path/'idempotency.json').items():
        monkeypatch.setattr(actions,key,value)
    manager=SimpleNamespace(unpin_message=AsyncMock(return_value=True))
    monkeypatch.setattr(actions,'ctx',SimpleNamespace(get_manager=AsyncMock(return_value=manager)))
    assert not (await actions.tg_unpin_message('other',1957981))['success']
    assert not (await actions.tg_unpin_message('5139858',0))['success']
    preview=await actions.tg_unpin_message('5139858',1957981)
    assert preview['dry_run'] and preview['approval_code']
    manager.unpin_message.assert_not_awaited()
    args=dict(group='5139858',message_id=1957981,dry_run=False,confirm=True,
              confirmation_text='exact-confirm',approval_code=preview['approval_code'])
    assert not (await actions.tg_unpin_message(**(args|{'confirmation_text':'wrong'})))['success']
    assert not (await actions.tg_unpin_message(**(args|{'message_id':1962485})))['success']
    assert (await actions.tg_unpin_message(**args))['success']
    manager.unpin_message.assert_awaited_once_with('5139858',1957981)
    assert not (await actions.tg_unpin_message(**args))['success']
