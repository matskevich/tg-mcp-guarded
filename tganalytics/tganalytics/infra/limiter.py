"""
Anti-Spam Rate Limiter для защиты от блокировок Telegram
==========================================

Основные компоненты:
- TokenBucket: Алгоритм ограничения скорости запросов (внутри процесса)
- RateLimiter: Основной класс управления лимитами
- safe_call: Wrapper для безопасных API вызовов с retry
- smart_pause: Интеллектуальные паузы для больших операций

Принцип: "Не считай минуты — считай RPC-токены"
Цель: 4 запроса/сек с автоматической обработкой FLOOD_WAIT
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from telethon.errors import FloodWaitError

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

from .paths import data_path, resolve_state_path
from .metrics import (
    increment_flood_wait_events_total,
    increment_rate_limit_requests_total,
    increment_rate_limit_throttled_total,
)

# Настройка логирования с тегом SAFE
logger = logging.getLogger(__name__)

_COUNTER_KEYS = ("dm_count", "join_count", "group_msg_count", "api_calls", "flood_waits")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class CircuitBreakerOpenError(Exception):
    """Raised when anti-spam circuit breaker is open."""

    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = max(0, int(seconds_remaining))
        super().__init__(
            f"[SAFE] Circuit breaker is open for {self.seconds_remaining}s due to recent long FLOOD_WAIT"
        )


class TokenBucket:
    """
    Token Bucket алгоритм для rate limiting внутри процесса

    Принцип работы:
    - Ведро имеет максимальную capacity (емкость)
    - Токены добавляются с постоянной скоростью refill_rate
    - При запросе тратится 1 токен
    - Если токенов нет - ждем их пополнения
    """

    def __init__(self, capacity: int = 10, refill_rate: float = 4.0):
        """
        Args:
            capacity: Максимальное количество токенов в ведре
            refill_rate: Скорость пополнения токенов в секунду (default: 4 RPS)
        """
        self.capacity = max(1, int(capacity))
        self.refill_rate = max(0.1, float(refill_rate))
        self.tokens = float(self.capacity)
        self.last_refill = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens_needed: int = 1) -> bool:
        """
        Получить токены из ведра

        Args:
            tokens_needed: Количество нужных токенов

        Returns:
            True если токены получены, False если недостаточно capacity
        """
        if tokens_needed > self.capacity:
            return False

        async with self._lock:
            now = time.time()
            time_passed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + time_passed * self.refill_rate)
            self.last_refill = now

            if self.tokens >= tokens_needed:
                self.tokens -= tokens_needed
                return True

            tokens_to_wait = tokens_needed - self.tokens
            wait_time = tokens_to_wait / self.refill_rate

            try:
                increment_rate_limit_throttled_total()
            except Exception:
                pass

            logger.info(f"[SAFE] Rate limit(local): waiting {wait_time:.2f}s for {tokens_needed} tokens")
            await asyncio.sleep(wait_time)

            self.tokens = min(self.capacity, self.tokens + wait_time * self.refill_rate)
            if self.tokens >= tokens_needed:
                self.tokens -= tokens_needed
                return True
            return False

    def get_wait_time(self, tokens_needed: int = 1) -> float:
        """Получить время ожидания для токенов без их получения"""
        if self.tokens >= tokens_needed:
            return 0.0
        tokens_to_wait = tokens_needed - self.tokens
        return tokens_to_wait / self.refill_rate


class RateLimiter:
    """
    Основной класс управления rate limiting для Telegram API

    Возможности:
    - Локальный rate limiting (token bucket в процессе)
    - Опциональный shared rate limiting между процессами
    - Квоты по операциям (DM, join/leave, group messages)
    - Персистентное хранение статистики
    """

    def __init__(
        self,
        rps: float = 4.0,
        max_dm_per_day: int = 20,
        max_joins_per_day: int = 20,
        max_group_msgs_per_day: int = 30,
        max_groups: int = 200,
        data_dir: str | None = None,
        global_rps_mode: str = "shared",
        flood_circuit_threshold_sec: int = 300,
        flood_circuit_cooldown_sec: int = 900,
    ):
        """
        Args:
            rps: Запросов в секунду
            max_dm_per_day: Максимум DM в сутки
            max_joins_per_day: Максимум join/leave в сутки
            max_group_msgs_per_day: Максимум сообщений в группы/каналы в сутки
            max_groups: Максимум групп для аккаунта
            data_dir: Директория для счетчиков (по умолчанию <repo>/data/anti_spam)
            global_rps_mode: shared|local|off
            flood_circuit_threshold_sec: FLOOD_WAIT threshold to trip circuit breaker
            flood_circuit_cooldown_sec: cooldown duration once circuit is tripped
        """
        self.rps = max(0.1, float(rps))
        self.max_dm_per_day = int(max_dm_per_day)
        self.max_joins_per_day = int(max_joins_per_day)
        self.max_group_msgs_per_day = int(max_group_msgs_per_day)
        self.max_groups = int(max_groups)

        self.data_dir = resolve_state_path(data_dir or "", "anti_spam")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.counter_file = self.data_dir / "daily_counters.txt"
        self.counter_lock_file = self.data_dir / "daily_counters.lock"

        self.global_rps_mode = (global_rps_mode or "shared").strip().lower()
        self.global_state_file = self.data_dir / "global_rps_state.json"
        self.global_lock_file = self.data_dir / "global_rps_state.lock"

        self.flood_circuit_threshold_sec = max(0, int(flood_circuit_threshold_sec))
        self.flood_circuit_cooldown_sec = max(0, int(flood_circuit_cooldown_sec))
        self.circuit_state_file = self.data_dir / "flood_circuit_state.json"
        self.circuit_lock_file = self.data_dir / "flood_circuit_state.lock"

        self.bucket = TokenBucket(capacity=int(self.rps * 2), refill_rate=self.rps)
        self.daily_counters = self._load_daily_counters()

        logger.info(
            "[SAFE] RateLimiter initialized: %.2f RPS, %d DM/day, %d joins/day, %d group_msgs/day, "
            "global_rps_mode=%s, circuit=%ss/%ss",
            self.rps,
            self.max_dm_per_day,
            self.max_joins_per_day,
            self.max_group_msgs_per_day,
            self.global_rps_mode,
            self.flood_circuit_threshold_sec,
            self.flood_circuit_cooldown_sec,
        )

    def _default_counters(self) -> Dict[str, Any]:
        base = {"date": _today_str()}
        for key in _COUNTER_KEYS:
            base[key] = 0
        return base

    def _acquire_file_lock(self, lock_file: Path):
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _release_file_lock(self, fd: int) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass

    def _normalize_counters(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        default = self._default_counters()
        if raw.get("date") != default["date"]:
            return default

        normalized = {"date": default["date"]}
        for key in _COUNTER_KEYS:
            try:
                normalized[key] = int(raw.get(key, 0))
            except Exception:
                normalized[key] = 0
        return normalized

    def _read_counters_file(self) -> Dict[str, Any]:
        if not self.counter_file.exists():
            return self._default_counters()

        parsed: Dict[str, Any] = {}
        try:
            with open(self.counter_file, "r", encoding="utf-8") as f:
                for line in f:
                    if "=" not in line:
                        continue
                    key, value = line.strip().split("=", 1)
                    parsed[key] = value
        except Exception as exc:
            logger.warning(f"[SAFE] Failed to read counters: {exc}, using defaults")
            return self._default_counters()

        return self._normalize_counters(parsed)

    def _save_counters_file_atomic(self, counters: Dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="daily_counters_", dir=str(self.data_dir), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"date={counters.get('date', _today_str())}\n")
                for key in _COUNTER_KEYS:
                    f.write(f"{key}={int(counters.get(key, 0))}\n")
            os.replace(tmp_path, self.counter_file)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _read_circuit_state_file(self) -> Dict[str, Any]:
        if not self.circuit_state_file.exists():
            return {"open_until": 0.0}
        try:
            raw = json.loads(self.circuit_state_file.read_text(encoding="utf-8"))
            return {"open_until": float(raw.get("open_until", 0.0))}
        except Exception:
            return {"open_until": 0.0}

    def _save_circuit_state_file_atomic(self, state: Dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="flood_circuit_", dir=str(self.data_dir), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"open_until": float(state.get("open_until", 0.0))}, f)
            os.replace(tmp_path, self.circuit_state_file)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _with_counters_lock(self, fn: Callable[[Dict[str, Any]], Any], persist: bool = False):
        fd = self._acquire_file_lock(self.counter_lock_file)
        try:
            counters = self._read_counters_file()
            result = fn(counters)
            self.daily_counters = counters
            if persist:
                self._save_counters_file_atomic(counters)
            return result
        finally:
            self._release_file_lock(fd)

    def _load_daily_counters(self) -> Dict[str, Any]:
        return self._with_counters_lock(lambda counters: counters.copy(), persist=False)

    async def _global_acquire(self, tokens_needed: int = 1) -> bool:
        """Shared token bucket between processes (same data_dir)."""
        if self.global_rps_mode in ("off", "local"):
            return True

        while True:
            fd = self._acquire_file_lock(self.global_lock_file)
            wait_time = 0.0
            try:
                state = {
                    "tokens": float(max(1, int(self.rps * 2))),
                    "last_refill": time.time(),
                }
                if self.global_state_file.exists():
                    try:
                        raw = json.loads(self.global_state_file.read_text(encoding="utf-8"))
                        state["tokens"] = float(raw.get("tokens", state["tokens"]))
                        state["last_refill"] = float(raw.get("last_refill", state["last_refill"]))
                    except Exception:
                        pass

                capacity = float(max(1, int(self.rps * 2)))
                now = time.time()
                elapsed = max(0.0, now - state["last_refill"])
                tokens = min(capacity, state["tokens"] + elapsed * self.rps)

                if tokens >= tokens_needed:
                    tokens -= tokens_needed
                    wait_time = 0.0
                else:
                    wait_time = (tokens_needed - tokens) / self.rps

                new_state = {"tokens": tokens, "last_refill": now}
                fd_tmp, tmp_path = tempfile.mkstemp(prefix="global_rps_", dir=str(self.data_dir), text=True)
                try:
                    with os.fdopen(fd_tmp, "w", encoding="utf-8") as f:
                        json.dump(new_state, f)
                    os.replace(tmp_path, self.global_state_file)
                finally:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
            finally:
                self._release_file_lock(fd)

            if wait_time <= 0:
                return True

            try:
                increment_rate_limit_throttled_total()
            except Exception:
                pass

            logger.info(f"[SAFE] Rate limit(shared): waiting {wait_time:.2f}s for {tokens_needed} tokens")
            await asyncio.sleep(wait_time)

    async def acquire(self, tokens_needed: int = 1) -> bool:
        """Acquire both local and optional shared tokens."""
        ok_local = await self.bucket.acquire(tokens_needed)
        if not ok_local:
            return False
        return await self._global_acquire(tokens_needed)

    async def check_circuit_breaker(self) -> None:
        """Raise when flood circuit is open."""
        fd = self._acquire_file_lock(self.circuit_lock_file)
        try:
            state = self._read_circuit_state_file()
            open_until = float(state.get("open_until", 0.0))
            now = time.time()

            if open_until <= 0:
                return

            if now >= open_until:
                state["open_until"] = 0.0
                self._save_circuit_state_file_atomic(state)
                logger.info("[SAFE] Circuit breaker closed after cooldown")
                return

            seconds_remaining = int(math.ceil(open_until - now))
            raise CircuitBreakerOpenError(seconds_remaining)
        finally:
            self._release_file_lock(fd)

    async def trip_circuit_breaker(self, flood_wait_seconds: int) -> None:
        """Open circuit breaker after critical FLOOD_WAIT."""
        if self.flood_circuit_threshold_sec <= 0:
            return
        if flood_wait_seconds < self.flood_circuit_threshold_sec:
            return

        fd = self._acquire_file_lock(self.circuit_lock_file)
        try:
            state = self._read_circuit_state_file()
            current_open_until = float(state.get("open_until", 0.0))
            now = time.time()
            open_until = now + self.flood_circuit_cooldown_sec
            if open_until > current_open_until:
                state["open_until"] = open_until
                self._save_circuit_state_file_atomic(state)
                logger.error(
                    "[SAFE] Circuit breaker OPEN for %ss (triggered by FLOOD_WAIT=%ss)",
                    self.flood_circuit_cooldown_sec,
                    flood_wait_seconds,
                )
        finally:
            self._release_file_lock(fd)

    def get_circuit_state(self) -> Dict[str, Any]:
        fd = self._acquire_file_lock(self.circuit_lock_file)
        try:
            state = self._read_circuit_state_file()
            open_until = float(state.get("open_until", 0.0))
            now = time.time()
            is_open = open_until > now
            if not is_open and open_until > 0:
                state["open_until"] = 0.0
                self._save_circuit_state_file_atomic(state)
            remaining = int(math.ceil(open_until - now)) if is_open else 0
        finally:
            self._release_file_lock(fd)

        return {
            "enabled": self.flood_circuit_threshold_sec > 0 and self.flood_circuit_cooldown_sec > 0,
            "open": is_open,
            "remaining_seconds": remaining,
            "threshold_seconds": self.flood_circuit_threshold_sec,
            "cooldown_seconds": self.flood_circuit_cooldown_sec,
        }

    async def check_dm_quota(self) -> bool:
        return self._with_counters_lock(
            lambda counters: int(counters.get("dm_count", 0)) < self.max_dm_per_day,
            persist=False,
        )

    async def check_join_quota(self) -> bool:
        return self._with_counters_lock(
            lambda counters: int(counters.get("join_count", 0)) < self.max_joins_per_day,
            persist=False,
        )

    async def check_group_msg_quota(self) -> bool:
        return self._with_counters_lock(
            lambda counters: int(counters.get("group_msg_count", 0)) < self.max_group_msgs_per_day,
            persist=False,
        )

    async def increment_dm_counter(self):
        def _update(counters: Dict[str, Any]):
            counters["dm_count"] = int(counters.get("dm_count", 0)) + 1

        self._with_counters_lock(_update, persist=True)
        logger.info(f"[SAFE] DM counter: {self.daily_counters['dm_count']}/{self.max_dm_per_day}")

    async def increment_join_counter(self):
        def _update(counters: Dict[str, Any]):
            counters["join_count"] = int(counters.get("join_count", 0)) + 1

        self._with_counters_lock(_update, persist=True)
        logger.info(f"[SAFE] Join counter: {self.daily_counters['join_count']}/{self.max_joins_per_day}")

    async def increment_group_msg_counter(self):
        def _update(counters: Dict[str, Any]):
            counters["group_msg_count"] = int(counters.get("group_msg_count", 0)) + 1

        self._with_counters_lock(_update, persist=True)
        logger.info(
            f"[SAFE] Group message counter: {self.daily_counters['group_msg_count']}/{self.max_group_msgs_per_day}"
        )

    async def increment_api_counter(self):
        def _update(counters: Dict[str, Any]):
            counters["api_calls"] = int(counters.get("api_calls", 0)) + 1

        self._with_counters_lock(_update, persist=True)
        if self.daily_counters["api_calls"] % 100 == 0:
            logger.info(f"[SAFE] API calls today: {self.daily_counters['api_calls']}")

    async def increment_flood_counter(self, wait_time: int):
        def _update(counters: Dict[str, Any]):
            counters["flood_waits"] = int(counters.get("flood_waits", 0)) + 1

        self._with_counters_lock(_update, persist=True)
        logger.warning(f"[SAFE] FLOOD_WAIT #{self.daily_counters['flood_waits']} for {wait_time}s")
        await self.trip_circuit_breaker(wait_time)

        if wait_time > 600:
            logger.error(f"[SAFE] CRITICAL: FLOOD_WAIT {wait_time}s - possible account risk!")

    def get_stats(self) -> Dict[str, Any]:
        current = self._load_daily_counters()
        return {
            "date": current.get("date"),
            "dm_usage": f"{current.get('dm_count', 0)}/{self.max_dm_per_day}",
            "join_usage": f"{current.get('join_count', 0)}/{self.max_joins_per_day}",
            "group_msg_usage": f"{current.get('group_msg_count', 0)}/{self.max_group_msgs_per_day}",
            "api_calls": current.get("api_calls", 0),
            "flood_waits": current.get("flood_waits", 0),
            "current_rps": self.rps,
            "global_rps_mode": self.global_rps_mode,
            "circuit_breaker": self.get_circuit_state(),
        }


# Глобальный экземпляр rate limiter
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Получить глобальный экземпляр rate limiter (Singleton pattern)"""
    global _rate_limiter
    if _rate_limiter is None:
        rps = float(os.getenv("RATE_RPS", "4.0"))
        max_dm = int(os.getenv("MAX_DM_PER_DAY", "20"))
        max_joins = int(os.getenv("MAX_JOINS_PER_DAY", "20"))
        max_group_msgs = int(os.getenv("MAX_GROUP_MSGS_PER_DAY", "30"))
        max_groups = int(os.getenv("MAX_GROUPS", "200"))
        global_rps_mode = os.getenv("TG_GLOBAL_RPS_MODE", "shared")
        flood_circuit_threshold_sec = int(os.getenv("TG_FLOOD_CIRCUIT_THRESHOLD_SEC", "300"))
        flood_circuit_cooldown_sec = int(os.getenv("TG_FLOOD_CIRCUIT_COOLDOWN_SEC", "900"))

        _rate_limiter = RateLimiter(
            rps=rps,
            max_dm_per_day=max_dm,
            max_joins_per_day=max_joins,
            max_group_msgs_per_day=max_group_msgs,
            max_groups=max_groups,
            global_rps_mode=global_rps_mode,
            flood_circuit_threshold_sec=flood_circuit_threshold_sec,
            flood_circuit_cooldown_sec=flood_circuit_cooldown_sec,
        )
    return _rate_limiter


async def safe_call(
    func: Callable,
    *args,
    max_retries: int = 3,
    operation_type: str = "api",
    timeout: float = 30.0,
    **kwargs,
) -> Any:
    """
    Безопасный wrapper для Telegram API вызовов с rate limiting и retry

    Args:
        func: Функция для вызова
        *args: Аргументы функции
        max_retries: Максимальное количество повторов
        operation_type: Тип операции ("api", "dm", "join", "group_msg") для квот
        timeout: Таймаут на один вызов в секундах (default: 30s)
        **kwargs: Keyword аргументы функции
    """
    limiter = get_rate_limiter()
    await limiter.check_circuit_breaker()

    if operation_type == "dm":
        if not await limiter.check_dm_quota():
            raise Exception(
                f"[SAFE] DM quota exceeded: {limiter.daily_counters.get('dm_count', 0)}/{limiter.max_dm_per_day}"
            )
    elif operation_type == "join":
        if not await limiter.check_join_quota():
            raise Exception(
                "[SAFE] Join quota exceeded: "
                f"{limiter.daily_counters.get('join_count', 0)}/{limiter.max_joins_per_day}"
            )
    elif operation_type == "group_msg":
        if not await limiter.check_group_msg_quota():
            raise Exception(
                "[SAFE] Group message quota exceeded: "
                f"{limiter.daily_counters.get('group_msg_count', 0)}/{limiter.max_group_msgs_per_day}"
            )

    retry_count = 0
    base_wait = 1.0

    while retry_count <= max_retries:
        try:
            await limiter.check_circuit_breaker()
            increment_rate_limit_requests_total()
            acquired = await limiter.acquire(1)
            if not acquired:
                raise Exception("[SAFE] Could not acquire rate limit token")

            await limiter.increment_api_counter()

            func_name = getattr(func, "__name__", "anonymous")
            logger.debug(
                "[SAFE] Calling %s (attempt %d/%d, timeout=%ss)",
                func_name,
                retry_count + 1,
                max_retries + 1,
                timeout,
            )
            try:
                result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error(f"[SAFE] Timeout ({timeout}s) calling {func_name}")
                raise asyncio.TimeoutError(f"API call {func_name} timed out after {timeout}s")

            if operation_type == "dm":
                await limiter.increment_dm_counter()
            elif operation_type == "join":
                await limiter.increment_join_counter()
            elif operation_type == "group_msg":
                await limiter.increment_group_msg_counter()

            return result

        except FloodWaitError as e:
            retry_count += 1
            wait_time = int(getattr(e, "seconds", 1))

            increment_flood_wait_events_total()
            await limiter.increment_flood_counter(wait_time)

            if retry_count > max_retries:
                logger.error(f"[SAFE] Max retries exceeded for {getattr(func, '__name__', 'anonymous')} after FLOOD_WAIT")
                raise e

            total_wait = wait_time + (base_wait * (2 ** (retry_count - 1)))
            logger.warning(
                "[SAFE] FLOOD_WAIT %ss + backoff %.1fs, retry %d/%d",
                wait_time,
                total_wait - wait_time,
                retry_count,
                max_retries,
            )
            await asyncio.sleep(total_wait)

        except CircuitBreakerOpenError:
            raise

        except Exception as e:
            logger.error(f"[SAFE] Error in {getattr(func, '__name__', 'anonymous')}: {e}")
            raise e

    raise Exception(f"[SAFE] Unexpected end of retry loop for {getattr(func, '__name__', 'anonymous')}")


async def smart_pause(operation_type: str, count: int = 1):
    """
    Интеллектуальные паузы для больших операций

    Args:
        operation_type: Тип операции ("participants", "dm_batch", "join_batch")
        count: Количество обработанных элементов
    """
    if operation_type == "participants":
        if count > 0 and count % 5000 == 0:
            logger.info(f"[SAFE] Smart pause: {count} participants processed, sleeping 1s")
            await asyncio.sleep(1.0)

    elif operation_type == "dm_batch":
        if count > 0 and count % 20 == 0:
            logger.info(f"[SAFE] Smart pause: {count} DMs sent, sleeping 60s")
            await asyncio.sleep(60.0)

    elif operation_type == "join_batch":
        if count > 0:
            logger.info(f"[SAFE] Smart pause: join/leave operation, sleeping 3s")
            await asyncio.sleep(3.0)


def setup_safe_logging():
    """Настройка логирования с тегом SAFE для мониторинга"""
    log_dir = data_path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    safe_log_file = log_dir / f"safe_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(safe_log_file)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)

    logger.info("[SAFE] Logging initialized")


# Инициализация при импорте
setup_safe_logging()
