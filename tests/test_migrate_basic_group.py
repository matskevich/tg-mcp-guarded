from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from telethon.tl.types import Chat, Channel, InputChannel
from telethon.tl.functions.messages import MigrateChatRequest
from tganalytics.domain.groups import GroupManager
from tganalytics.infra.tele_client import _is_telethon_write_request
import mcp_server_actions as actions


def setup_manager(admin=True):
    old = Chat(id=123, title='test', photo=None, participants_count=1,
               date=datetime.now(timezone.utc), version=1)
    new = Channel(id=456, title='test', photo=None,
                  date=datetime.now(timezone.utc), megagroup=True)
    migrated = Chat(id=123, title='test', photo=None, participants_count=1,
                    date=datetime.now(timezone.utc), version=2,
                    migrated_to=InputChannel(456, 0))
    client = AsyncMock()
    client.get_entity.return_value = old
    client.get_permissions.return_value = NS(is_creator=admin, is_admin=admin)
    client.get_participants.return_value = [NS(id=1)]
    client.side_effect = [NS(chats=[migrated, new]),
                          NS(chats=[new], full_chat=NS(migrated_from_chat_id=123))]
    return GroupManager(client), client, old, new

@pytest.mark.asyncio
async def test_preview_does_not_migrate():
    m,c,_,_=setup_manager()
    r=await m.migrate_basic_group('-123','test',True)
    assert r['success'] and r['irreversible']
    c.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize('admin,title',[(False,'test'),(True,'wrong')])
async def test_reject_admin_or_title(admin,title):
    m,c,_,_=setup_manager(admin)
    with pytest.raises(ValueError):await m.migrate_basic_group('-123',title,False)
    c.assert_not_awaited()

@pytest.mark.asyncio
async def test_migrate_and_readback():
    m,c,_,_=setup_manager()
    r=await m.migrate_basic_group('-123','test',False)
    assert r['success'] and r['read_back_verified']
    assert r['new_group_id']==-1000000000456
    assert isinstance(c.await_args_list[0].args[0],MigrateChatRequest)

@pytest.mark.asyncio
async def test_public_readback_not_success():
    m,c,_,new=setup_manager();new.username='public'
    r=await m.migrate_basic_group('-123','test',False)
    assert r['migration_submitted'] and not r['success']

@pytest.mark.asyncio
async def test_already_migrated_never_repeats():
    m,c,old,_=setup_manager();old.migrated_to=InputChannel(456,0)
    r=await m.migrate_basic_group('-123','test',False)
    assert r['already_migrated'];c.assert_not_awaited()

@pytest.mark.asyncio
async def test_readback_failure_preserves_submitted():
    m,c,_,_=setup_manager();first=c.side_effect
    c.side_effect=[NS(chats=[])]
    r=await m.migrate_basic_group('-123','test',False)
    assert r['migration_submitted'] and not r['read_back_verified']


def test_rpc_is_write_guarded():
    assert _is_telethon_write_request(MigrateChatRequest(123))

@pytest.mark.asyncio
async def test_wrapper_precondition_blocks(monkeypatch):
    monkeypatch.setattr(actions,'_check_action_preconditions',lambda *a,**k:(False,'denied'))
    r=await actions.tg_migrate_basic_group('-123','test')
    assert not r['success']

@pytest.mark.asyncio
async def test_wrapper_approval_and_uncertain_duplicate(monkeypatch):
    monkeypatch.setattr(actions,'_check_action_preconditions',lambda *a,**k:(True,None))
    manager=NS(migrate_basic_group=AsyncMock(side_effect=[{'success':True},RuntimeError('timeout')]))
    monkeypatch.setattr(actions,'ctx',NS(get_manager=AsyncMock(return_value=manager)))
    monkeypatch.setattr(actions,'_approval_gate',lambda **k:(False,'bad code',{}))
    assert not (await actions.tg_migrate_basic_group('-123','test',False))['success']
    manager.migrate_basic_group.assert_not_awaited()
    monkeypatch.setattr(actions,'_approval_gate',lambda **k:(True,None,{}))
    recorded=[]
    monkeypatch.setattr(actions,'_check_recent_duplicate',lambda h:(bool(recorded),10))
    monkeypatch.setattr(actions,'_mark_action_executed',lambda h:recorded.append(h))
    r=await actions.tg_migrate_basic_group('-123','test',False)
    assert r['reconciliation_required'] and recorded
    assert (await actions.tg_migrate_basic_group('-123','test',False))['duplicate_blocked']
