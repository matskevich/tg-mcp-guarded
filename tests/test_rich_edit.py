from copy import deepcopy
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from telethon.tl.functions.messages import EditMessageRequest
from test_rich_publication import action_api, content
import mcp_rich_edit as edit
from mcp_rich_publication import ledger, reconcile_rich


def make_edit_case(tmp_path, monkeypatch):
    calls = []
    api = action_api(tmp_path, calls)
    api['_check_target_allowed'] = lambda *_: (True, None)
    raw = dict(id=7, peer_id={'_':'PeerUser', 'user_id':10},
               rich_message=content('old'), reply_to={'reply_to_msg_id':3})
    before = dict(success=True, content_complete=True, format='rich',
                  rich_sha256='a'*64, raw_message=raw)
    read = AsyncMock(return_value=before)
    monkeypatch.setattr(edit, 'full_message', read)
    args = dict(group='10', message_id=7, rich_message=content('new'),
                operation_id='synthetic-edit', expected_actor_id=10,
                expected_sender_id=10, expected_rich_sha256='a'*64)
    return api, args, read, before, calls


@pytest.mark.asyncio
async def test_unknown_edit_is_fenced_after_restart_and_independently_reconciled(tmp_path, monkeypatch):
    api, args, read, before, calls = make_edit_case(tmp_path, monkeypatch)
    preview = await edit.edit_rich(api, **args)
    assert preview['success'] and not calls
    result = await edit.edit_rich(api, **args, dry_run=False, confirm=True,
                                 confirmation_text='synthetic', approval_code='code')
    assert result['state'] == 'unknown'
    assert len(calls) == 1 and isinstance(calls[0], EditMessageRequest)
    assert calls[0].id == 7 and calls[0].message is None
    # New connection, no in-memory dispatch state: unknown remains fenced.
    again = await edit.edit_rich(api, **args)
    assert again['new_dispatch'] is False and len(calls) == 1
    changed = dict(args, rich_message=content('different'))
    assert not (await edit.edit_rich(api, **changed))['success']
    after = deepcopy(before)
    after['raw_message']['rich_message'] = content('new')
    after['rich_sha256'] = 'b'*64
    read.return_value = after
    verified = await edit.reconcile_rich_edit(api, '10', args['operation_id'], 7)
    assert verified['state'] == 'verified' and verified['independent_readback']
    assert len(calls) == 1
    assert not (await reconcile_rich(api, '10', args['operation_id'], 7))['success']
    after['raw_message']['reply_to'] = {'reply_to_msg_id':4}
    assert (await edit.reconcile_rich_edit(api, '10', args['operation_id'], 7))['readback_mismatch']


@pytest.mark.asyncio
async def test_version_change_after_approval_prevents_dispatch(tmp_path, monkeypatch):
    api, args, read, before, calls = make_edit_case(tmp_path, monkeypatch)
    assert (await edit.edit_rich(api, **args))['success']
    changed = deepcopy(before)
    changed['rich_sha256'] = 'c'*64
    read.side_effect = [before, changed]
    result = await edit.edit_rich(api, **args, dry_run=False, confirm=True,
                                 confirmation_text='synthetic', approval_code='code')
    assert result['error_code'] == 'before_version_changed_before_dispatch'
    assert not calls
    db = ledger(api)
    assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['actor', 'sender', 'version', 'partial', 'target', 'policy'])
async def test_preview_rejects_wrong_identity_or_incomplete_before(tmp_path, monkeypatch, change):
    api, args, read, before, calls = make_edit_case(tmp_path, monkeypatch)
    if change == 'actor': args['expected_actor_id'] = 11
    if change == 'sender': args['expected_sender_id'] = 11
    if change == 'version': args['expected_rich_sha256'] = 'c'*64
    if change == 'partial': before['content_complete'] = False
    if change == 'target': args['group'] = '11'
    if change == 'policy': api['_check_action_preconditions'] = lambda *a, **k: (False, 'policy')
    assert not (await edit.edit_rich(api, **args))['success']
    assert not calls


@pytest.mark.asyncio
async def test_successful_rpc_still_requires_readback_and_preserves_original_id(tmp_path, monkeypatch):
    api, args, read, before, calls = make_edit_case(tmp_path, monkeypatch)
    rpc = AsyncMock(return_value=SimpleNamespace(updates=[]))
    monkeypatch.setattr(edit, 'safe_call', rpc)
    assert (await edit.edit_rich(api, **args))['success']
    result = await edit.edit_rich(api, **args, dry_run=False, confirm=True,
                                 confirmation_text='synthetic', approval_code='code')
    assert result['message_id'] == 7 and result['effect_verified'] is False
    assert rpc.call_args.kwargs['max_retries'] == 0
    assert (await edit.reconcile_rich_edit(api, '10', args['operation_id'], 7))['readback_mismatch']


@pytest.mark.asyncio
async def test_new_operation_cannot_bypass_unknown_edit_fence(tmp_path, monkeypatch):
    api, args, read, before, calls = make_edit_case(tmp_path, monkeypatch)
    await edit.edit_rich(api, **args)
    await edit.edit_rich(api, **args, dry_run=False, confirm=True,
                         confirmation_text='synthetic', approval_code='code')
    another = dict(args, operation_id='another-operation')
    await edit.edit_rich(api, **another)
    result = await edit.edit_rich(api, **another, dry_run=False, confirm=True,
                                 confirmation_text='synthetic', approval_code='code')
    assert result['error_code'] == 'message_has_unreconciled_edit'
    assert len(calls) == 1
