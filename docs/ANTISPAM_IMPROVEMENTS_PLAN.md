# План улучшений антиспам-системы

Дата: 2026-01-28
Статус: backlog (делаем когда решим)

---

## P0 — критично

### 1. Circuit breaker

**Проблема:** при FLOOD_WAIT 600+ сек система логирует CRITICAL, но продолжает retry. Нет механизма остановки всех вызовов.

**Решение:** добавить в `RateLimiter` состояние `tripped` — если FLOOD_WAIT > порога (например, 300с), все последующие `safe_call` сразу бросают ошибку на N минут. Автоматический reset после cooldown.

**Файл:** `tganalytics/tganalytics/infra/limiter.py`

---

## P1 — важно перед масштабированием

### 2. Хрупкое определение тестового окружения

**Проблема:** `_is_testing_environment()` в `domain/groups.py` проверяет `'test' in arg.lower()` — сработает на любом файле с "test" в имени, даже в проде.

**Решение:** dependency injection — передавать `rate_limiter=None` для тестов, или явный env-флаг `DISABLE_RATE_LIMIT=1`.

**Файл:** `tganalytics/tganalytics/domain/groups.py:18-26`

### 3. Compliance checker не ловит raw MTProto

**Проблема:** сканер ищет `client.get_entity`, `client.iter_participants`, но не видит `await self.client(GetFullChannelRequest(...))` — прямой MTProto-вызов без safe_call. Такой вызов есть прямо в `groups.py:80-81`.

**Решение:** добавить паттерн `await\s+(?:self\.)?client\(` в `DANGEROUS_PATTERNS`.

**Файл:** `scripts/check_anti_spam_compliance.py`

---

## P2 — полезно, не горит

### 4. Персистентность метрик

**Проблема:** счётчики `rate_limit_requests_total` и т.д. обнуляются при каждом перезапуске. Нет истории.

**Решение:** при завершении процесса дампить `snapshot()` в файл (JSON/SQLite). Или Prometheus endpoint если появится web-сервер.

**Файл:** `tganalytics/tganalytics/infra/metrics.py`

### 5. Атомарная запись daily_counters

**Проблема:** `daily_counters.txt` пишется напрямую — при падении процесса файл может обрезаться.

**Решение:** write-to-temp + `os.rename()` (атомарная операция на одной ФС).

**Файл:** `tganalytics/tganalytics/infra/limiter.py:188-196`

### 6. Один bucket на процесс, не на аккаунт

**Проблема:** при работе с двух аккаунтов (example_session + example_account) из одного процесса, TokenBucket ограничит суммарный RPS до 4, хотя можно 4+4=8.

**Решение:** привязать bucket к session path в `_clients_by_path`.

**Файл:** `tganalytics/tganalytics/infra/tele_client.py`
