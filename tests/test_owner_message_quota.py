"""An owner-configured daily cap opt-out preserves accounting and other gates."""
from unittest.mock import AsyncMock

import pytest

from tganalytics.infra import limiter as module


@pytest.mark.asyncio
async def test_owner_continues_after_30_and_restart(tmp_path, monkeypatch):
    limiter = module.RateLimiter(data_dir=str(tmp_path), max_group_msgs_per_day=0)
    for _ in range(30):
        await limiter.increment_group_msg_counter()
    monkeypatch.setattr(module, "_rate_limiter", limiter)
    send = AsyncMock(return_value="sent")

    assert await module.safe_call(send, operation_type="group_msg") == "sent"
    assert limiter.get_stats()["group_msg_usage"] == "31/unlimited"
    restored = module.RateLimiter(data_dir=str(tmp_path), max_group_msgs_per_day=0)
    assert await restored.check_group_msg_quota()
    assert restored.get_stats()["group_msg_daily_limit"] is None


@pytest.mark.asyncio
async def test_default_profile_still_has_a_daily_cap(tmp_path):
    limiter = module.RateLimiter(data_dir=str(tmp_path))
    for _ in range(30):
        await limiter.increment_group_msg_counter()

    assert not await limiter.check_group_msg_quota()
    assert limiter.get_stats()["group_msg_daily_limit"] == 30


@pytest.mark.asyncio
async def test_owner_still_obeys_flood_circuit(tmp_path, monkeypatch):
    limiter = module.RateLimiter(data_dir=str(tmp_path), max_group_msgs_per_day=0)
    monkeypatch.setattr(module, "_rate_limiter", limiter)
    await limiter.trip_circuit_breaker(600)
    send = AsyncMock()

    with pytest.raises(module.CircuitBreakerOpenError):
        await module.safe_call(send, operation_type="group_msg")
    send.assert_not_called()


def test_negative_cap_is_invalid(tmp_path):
    with pytest.raises(ValueError):
        module.RateLimiter(data_dir=str(tmp_path), max_group_msgs_per_day=-1)
