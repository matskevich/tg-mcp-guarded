import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import User

from tganalytics.domain.groups import GroupManager
from tganalytics.infra.reply_budget import ReplyBudget, LocalQuotaExceeded
from tganalytics.infra import limiter


def test_reply_budget_shared_caps_and_reset(tmp_path):
    a = ReplyBudget(tmp_path, total=3, per_target=2, interval=10)
    b = ReplyBudget(tmp_path, total=3, per_target=2, interval=10)
    a.reserve(123, now=1000)
    with pytest.raises(LocalQuotaExceeded, match='interval'):
        b.reserve(123, now=1001)
    b.reserve(123, now=1011)
    with pytest.raises(LocalQuotaExceeded, match='per_target'):
        a.reserve(123, now=1022)
    a.reserve(456, now=1022)
    with pytest.raises(LocalQuotaExceeded, match='daily'):
        b.reserve(789, now=1033)
    b.reserve(123, now=86400+1000)
    assert b.stats(now=86400+1001)['used'] == 1


def test_corrupt_budget_fails_closed(tmp_path):
    budget = ReplyBudget(tmp_path)
    budget.path.write_text('not json')
    with pytest.raises(LocalQuotaExceeded, match='state'):
        budget.reserve(123)


@pytest.mark.asyncio
async def test_recent_incoming_qualifies_but_outgoing_does_not():
    client = AsyncMock()
    manager = GroupManager(client)
    user = User(id=123, first_name='test')
    msg = SimpleNamespace(out=False, sender_id=123, date=datetime.now(timezone.utc))
    client.get_messages.return_value = [msg]
    assert await manager._is_recent_conversation(user)
    msg.out = True
    assert not await manager._is_recent_conversation(user)
    msg.out = False
    msg.date -= timedelta(days=8)
    assert not await manager._is_recent_conversation(user)
    msg.date = datetime.now(timezone.utc)
    msg.sender_id = 456
    assert not await manager._is_recent_conversation(user)
    user.bot = True
    assert not await manager._is_recent_conversation(user)


@pytest.mark.asyncio
async def test_reply_survives_old_quota_but_keeps_circuit(tmp_path, monkeypatch):
    rl = limiter.RateLimiter(data_dir=str(tmp_path), max_group_msgs_per_day=0)
    monkeypatch.setattr(limiter, '_rate_limiter', rl)
    fn = AsyncMock(return_value='sent')
    assert await limiter.safe_call(fn, operation_type='conversation_reply', operation_target_id=123) == 'sent'
    with pytest.raises(LocalQuotaExceeded):
        await limiter.safe_call(fn, operation_type='group_msg')
    assert fn.await_count == 1
    await rl.trip_circuit_breaker(600)
    with pytest.raises(limiter.CircuitBreakerOpenError):
        await limiter.safe_call(fn, operation_type='conversation_reply', operation_target_id=456)
    assert fn.await_count == 1


@pytest.mark.asyncio
async def test_reservation_kept_after_uncertain_send(tmp_path, monkeypatch):
    rl = limiter.RateLimiter(data_dir=str(tmp_path))
    monkeypatch.setattr(limiter, '_rate_limiter', rl)
    fn = AsyncMock(side_effect=asyncio.TimeoutError)
    with pytest.raises(asyncio.TimeoutError):
        await limiter.safe_call(fn, operation_type='conversation_reply', operation_target_id=123)
    assert rl.get_stats()['conversation_reply_budget']['used'] == 1


@pytest.mark.asyncio
async def test_real_manager_routes_reply_and_blocks_cold_send(tmp_path, monkeypatch):
    monkeypatch.setenv('TG_DISABLE_RATE_LIMIT_FOR_TESTS', '0')
    rl = limiter.RateLimiter(data_dir=str(tmp_path), max_group_msgs_per_day=0)
    monkeypatch.setattr(limiter, '_rate_limiter', rl)
    client = AsyncMock()
    manager = GroupManager(client)
    user = User(id=123, first_name='test')
    manager._resolve_target_entity = AsyncMock(return_value=user)
    client.get_messages.return_value = [SimpleNamespace(out=False, sender_id=123, date=datetime.now(timezone.utc))]
    assert await manager.send_message('test_user', 'reply')
    assert client.send_message.await_count == 1
    with pytest.raises(LocalQuotaExceeded, match='interval'):
        await manager.send_message('test_user', 'second reply')
    client.get_messages.return_value = []
    with pytest.raises(LocalQuotaExceeded, match='Group message quota'):
        await manager.send_message('test_user', 'cold send')
    assert client.send_message.await_count == 1


def test_atomic_reservations_across_instances(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def attempt(i):
        try:
            ReplyBudget(tmp_path, total=3, interval=0).reserve(i+1)
            return True
        except LocalQuotaExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(12))) == 3


@pytest.mark.asyncio
async def test_history_failure_never_sends():
    client = AsyncMock()
    manager = GroupManager(client)
    manager._resolve_target_entity = AsyncMock(return_value=User(id=123))
    client.get_messages.side_effect = RuntimeError('history unavailable')
    assert not await manager.send_message('test_user', 'reply')
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_returns_local_quota_without_marking_delivery(monkeypatch):
    import mcp_server_actions as actions
    monkeypatch.setattr(actions, 'SAFE_STARTUP_BLOCK_REASON', None)
    monkeypatch.setattr(actions, 'ACTIONS_ENABLED', True)
    monkeypatch.setattr(actions, 'REQUIRE_ALLOWLIST', True)
    monkeypatch.setattr(actions, 'ALLOWED_TARGETS', {'test_target'})
    monkeypatch.setattr(actions, 'REQUIRE_CONFIRMATION_TEXT', True)
    monkeypatch.setattr(actions, 'CONFIRMATION_PHRASE', 'confirm-test-action')
    monkeypatch.setattr(actions, 'REQUIRE_APPROVAL_CODE', False)
    monkeypatch.setattr(actions, 'IDEMPOTENCY_ENABLED', False)
    manager = AsyncMock()
    manager.send_message.side_effect = LocalQuotaExceeded('conversation_reply: daily budget 100/100')
    monkeypatch.setattr(actions.ctx, 'get_manager', AsyncMock(return_value=manager))
    mark = AsyncMock()
    monkeypatch.setattr(actions, '_mark_action_executed', mark)
    result = await actions.tg_send_message(group='test_target', message_text='reply', dry_run=False,
                                         confirm=True, confirmation_text='confirm-test-action')
    assert result['error_code'] == 'local_quota_exceeded'
    assert result['sent'] is False
    mark.assert_not_called()
