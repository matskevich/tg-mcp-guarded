from types import SimpleNamespace as S
from unittest.mock import AsyncMock, Mock

import pytest

from mcp_reactions import register_actions, read_reactions


def namespace(own=None):
    tools = {}
    class MCP:
        def tool(self):
            def register(fn):
                tools[fn.__name__] = fn
                return fn
            return register
    entity = S()
    ctx = S(client=S(get_messages=Mock()), get_manager=AsyncMock(return_value=S(
        _resolve_read_entity=AsyncMock(return_value=entity),
        _resolve_target_entity=AsyncMock(return_value=entity))))
    reactions = None if own is None else S(results=[S(reaction=S(emoticon=own), chosen_order=0)])
    safe = AsyncMock(return_value=S(id=42, reactions=reactions))
    ns = {'mcp': MCP(), 'ctx': ctx, 'safe_call': safe,
          '_check_action_preconditions': Mock(return_value=(True, None)),
          '_blocked': lambda error, **kw: {'success': False, 'error': error, **kw},
          '_normalize_target': str, '_hash_payload': lambda p: repr(p),
          '_approval_gate': Mock(return_value=(True, None, {'approval_code': 'test'})),
          '_check_recent_duplicate': Mock(return_value=(False, 0)),
          '_mark_action_executed': Mock()}
    register_actions(ns)
    return ns, tools['tg_acknowledge_message']


@pytest.mark.asyncio
async def test_dry_run_never_calls_telegram():
    ns, fn = namespace()
    assert (await fn('20', 42, '👾'))['approval_code'] == 'test'
    ns['safe_call'].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['_check_action_preconditions', '_approval_gate'])
async def test_action_gates_block_before_rpc(gate):
    ns, fn = namespace()
    ns[gate].return_value = (False, 'blocked') if gate.endswith('preconditions') else (False, 'blocked', None)
    assert not (await fn('20', 42, '👾', dry_run=False))['success']
    ns['safe_call'].assert_not_called()


@pytest.mark.asyncio
async def test_existing_human_reaction_preserved():
    ns, fn = namespace('❤')
    result = await fn('20', 42, '👾', dry_run=False)
    assert result['preserved_existing'] and not result['written']
    assert ns['safe_call'].await_count == 1
    ns['_mark_action_executed'].assert_not_called()


@pytest.mark.asyncio
async def test_write_uses_limiter_and_fences_before_rpc():
    ns, fn = namespace()
    async def call(*args, **kw):
        if kw['operation_type'] == 'group_msg':
            ns['_mark_action_executed'].assert_called_once()
            assert type(args[1]).__name__ == 'SendReactionRequest'
            raise TimeoutError()
        return S(id=42, reactions=None)
    ns['safe_call'].side_effect = call
    assert (await fn('20', 42, '👾', dry_run=False))['reconciliation_required']


@pytest.mark.asyncio
async def test_missing_message_is_unknown_not_empty_reactions():
    ns, _ = namespace()
    ns['safe_call'].return_value = None
    with pytest.raises(ValueError, match='unavailable'):
        await read_reactions(ns, '20', 42)


@pytest.mark.asyncio
async def test_exact_annotations_expose_quote_and_external_reply_without_writes():
    import hashlib
    from telethon.tl.types import MessageEntityBlockquote, PeerChannel
    from mcp_reactions import register_reads
    ns, _ = namespace()
    ns['safe_call'].return_value = S(id=42, sender_id=20, message='агентик помоги',
        entities=[MessageEntityBlockquote(offset=0, length=14)],
        reply_to=S(reply_to_msg_id=7, reply_to_peer_id=PeerChannel(channel_id=99)),
        fwd_from=None, via_bot_id=None)
    tools = {}
    class MCP:
        def tool(self):
            def bind(fn):
                tools[fn.__name__] = fn
                return fn
            return bind
    ns['mcp'] = MCP()
    register_reads(ns)
    result = await tools['tg_get_message_annotations']('20', 42)
    assert result['leading_quote'] and result['sender_id'] == 20
    assert result['reply_peer_id'] == -1000000000099
    assert result['text_sha256'] == hashlib.sha256('агентик помоги'.encode()).hexdigest()
    assert ns['safe_call'].call_args.kwargs['operation_type'] == 'api'
