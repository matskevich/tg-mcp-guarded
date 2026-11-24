from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from telethon.tl.types import Channel, User, ChatAdminRights
from telethon.tl.functions.channels import EditAdminRequest
from tganalytics.infra.tele_client import _is_telethon_write_request
import mcp_channel_publishing as mod


class MCP:
    def tool(self):
        return lambda fn: fn


def setup(role='ChannelParticipant', rights=None):
    channel = Channel(id=1234, title='test', photo=None,
                      date=datetime.now(timezone.utc), broadcast=True)
    member = User(id=99, first_name='test', bot=False)
    client = AsyncMock()
    client.get_me.return_value = NS(id=10)
    client.get_entity.side_effect = lambda ident: channel if ident == -1000000001234 else member
    def participant():
        cls = type(role, (), {})
        p = cls(); p.user_id=99; p.admin_rights=rights; p.rank=None
        return p
    async def rpc(req):
        nonlocal role, rights
        if isinstance(req, EditAdminRequest):
            role='ChannelParticipantAdmin'; rights=req.admin_rights
            return NS()
        if req.participant == 'me':
            return NS(participant=type('ChannelParticipantCreator', (), {})())
        return NS(participant=participant())
    client.side_effect = rpc
    ctx = NS(get_manager=AsyncMock(return_value=NS(client=client)))
    marks=[]
    api={'mcp':MCP(), 'ctx':ctx, '_blocked':lambda e:{'success':False,'error':e},
         '_check_action_preconditions':lambda *a,**k:(True,None),
         '_hash_payload':lambda p:mod.role_digest(p),
         '_approval_gate':lambda **k:(True,None,{'approval_code':'code'}),
         '_check_recent_duplicate':lambda h:(h in marks,100),
         '_mark_action_executed':lambda h:marks.append(h),
         'CONFIRMATION_PHRASE':'confirmed', 'REQUIRE_CONFIRMATION_TEXT':True}
    return client,ctx,api,marks


async def args_for(ctx):
    _,_,_,r=await mod.inspect_member(ctx,'-1000000001234','99',-1000000001234,99,10)
    return dict(group='-1000000001234',user='99',expected_channel_id=-1000000001234,
                expected_user_id=99,expected_actor_id=10,expected_state_hash=r['state_hash'])


@pytest.mark.asyncio
async def test_read_and_preview_never_write():
    c,ctx,api,marks=setup();args=await args_for(ctx)
    read=mod.register_read(MCP(),ctx)
    r=await read(**{k:v for k,v in args.items() if k!='expected_state_hash'})
    assert r['success'] and r['admin_rights']=={}
    r=await mod.register_actions(api)(**args)
    assert r['success'] and r['dry_run'] and r['approval_code']=='code'
    assert not marks and not any(isinstance(x.args[0],EditAdminRequest) for x in c.await_args_list)


@pytest.mark.asyncio
async def test_grant_only_publish_and_readback():
    c,ctx,api,marks=setup();args=await args_for(ctx)
    r=await mod.register_actions(api)(**args,dry_run=False,confirm=True,confirmation_text='confirmed',approval_code='code')
    assert r['success'] and r['read_back_verified'] and marks
    writes=[x.args[0] for x in c.await_args_list if isinstance(x.args[0],EditAdminRequest)]
    assert len(writes)==1
    assert {k for k,v in mod.rights_dict(writes[0].admin_rights).items() if v}=={'post_messages'}
    assert _is_telethon_write_request(writes[0])
    assert (await mod.register_actions(api)(**args,dry_run=False))['duplicate_blocked']


@pytest.mark.asyncio
async def test_broad_role_and_stale_state_refused():
    c,ctx,api,marks=setup('ChannelParticipantAdmin',ChatAdminRights(post_messages=True,add_admins=True))
    args=await args_for(ctx); r=await mod.register_actions(api)(**args,dry_run=False)
    assert not r['success'] and 'outside minimal' in r['error'] and not marks
    args['expected_state_hash']='0'*64
    r=await mod.register_actions(api)(**args,dry_run=False)
    assert not r['success'] and 'Role changed' in r['error']


@pytest.mark.asyncio
async def test_guards_block_before_manager():
    c,ctx,api,_=setup();args=await args_for(ctx);ctx.get_manager.reset_mock()
    api['_check_action_preconditions']=lambda *a,**k:(False,'allowlist denied')
    assert not (await mod.register_actions(api)(**args,dry_run=False))['success']
    ctx.get_manager.assert_not_awaited()
    api['_check_action_preconditions']=lambda *a,**k:(True,None)
    api['_approval_gate']=lambda **k:(False,'bad code',{})
    assert not (await mod.register_actions(api)(**args,dry_run=False))['success']
    ctx.get_manager.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_effect_reserved_and_not_retried():
    c,ctx,api,marks=setup();args=await args_for(ctx);original=c.side_effect
    async def rpc(req):
        if isinstance(req,EditAdminRequest):raise TimeoutError('unknown outcome')
        return await original(req)
    c.side_effect=rpc
    r=await mod.register_actions(api)(**args,dry_run=False)
    assert r['submitted'] and r['reconciliation_required'] and marks
    assert (await mod.register_actions(api)(**args,dry_run=False))['duplicate_blocked']


@pytest.mark.asyncio
async def test_identity_actor_channel_and_member():
    c,ctx,api,_=setup()
    for ids in [(-1000000001234,99,11),(-1000000009999,99,10),(-1000000001234,98,10)]:
        with pytest.raises(ValueError):
            await mod.inspect_member(ctx,'-1000000001234','99',*ids)


@pytest.mark.asyncio
async def test_revoke_minimal_and_noop():
    c,ctx,api,_=setup('ChannelParticipantAdmin',ChatAdminRights(post_messages=True))
    args=await args_for(ctx);fn=mod.register_actions(api)
    r=await fn(**args,dry_run=False)
    assert r['no_op'] and not any(isinstance(x.args[0],EditAdminRequest) for x in c.await_args_list)
    r=await fn(**args,enabled=False,dry_run=False)
    assert r['success'] and not r['after']['admin_rights'].get('post_messages')


@pytest.mark.asyncio
async def test_server_extra_right_can_only_rollback_recorded_exact_grant():
    c,ctx,api,marks=setup();original_args=await args_for(ctx)
    fn=mod.register_actions(api);original_rpc=c.side_effect
    async def extra_right(req):
        if isinstance(req,EditAdminRequest) and req.admin_rights.post_messages:
            req.admin_rights.ban_users=True
        return await original_rpc(req)
    c.side_effect=extra_right
    r=await fn(**original_args,dry_run=False)
    assert not r['success'] and r['submitted'] and r['reconciliation_required']
    args=await args_for(ctx)
    # A broad role alone does not authorize revocation.
    assert not (await fn(**args,enabled=False,dry_run=False))['success']
    assert not (await fn(**args,enabled=False,rollback_of_state_hash='0'*64,dry_run=False))['success']
    r=await fn(**args,enabled=False,rollback_of_state_hash=original_args['expected_state_hash'],dry_run=False)
    assert r['success'] and not r['after']['admin_rights'].get('post_messages')
