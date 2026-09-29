# Антиспам и безопасность в tganalytics

## Зачем это нужно

Telegram банит аккаунты за слишком частые API-вызовы. Бан выглядит как `FLOOD_WAIT` (ждать N секунд), а в тяжёлых случаях — полная блокировка аккаунта. Вся система построена вокруг одной идеи: **ни один Telegram API вызов не должен пройти мимо rate limiter**.

## Архитектура: 5 уровней защиты

```
[Проектный код: gconf/, vahue/]
         │
         ▼
   _safe_api_call()          ← (1) точка входа, адаптер для тестов/прода
         │
         ▼
      safe_call()            ← (2) retry + backoff + квоты
         │
         ▼
    TokenBucket.acquire()    ← (3) rate limiting: 4 RPS
         │
         ▼
    Telegram API             ← (4) если FLOOD_WAIT → retry loop
         │
         ▼
     smart_pause()           ← (5) интеллектуальные паузы для bulk-операций
```

## Уровень 1: Token Bucket — контроль скорости

Файл: `tganalytics/tganalytics/infra/limiter.py`

Классический алгоритм «ведро с токенами»:
- Ведро на **10 токенов**, пополняется со скоростью **4 токена/сек**
- Каждый API-вызов тратит 1 токен
- Если токенов нет — `asyncio.sleep()` до пополнения, а не отказ
- Конфигурация через `.env`: `RATE_RPS=4.0`

Результат: средний RPS не превышает 4, burst до 10 запросов подряд допустим.

## Уровень 2: safe_call — retry + квоты

Файл: `tganalytics/tganalytics/infra/limiter.py`

Враппер для каждого API-вызова:
1. **Проверка квот перед вызовом**: для `dm` — не более 20/день, для `join` — не более 20/день, для `group_msg` — не более `MAX_GROUP_MSGS_PER_DAY`
2. **`bucket.acquire(1)`** — ждём токен
3. **Вызов функции**
4. **При `FloodWaitError`** — exponential backoff: `wait_time_от_TG + base * 2^retry`, до 3 попыток
5. **Circuit breaker** — при длинном FLOOD_WAIT (`TG_FLOOD_CIRCUIT_THRESHOLD_SEC`) все новые вызовы блокируются на cooldown (`TG_FLOOD_CIRCUIT_COOLDOWN_SEC`)
5. **При любой другой ошибке** — сразу throw, без retry

```python
total_wait = wait_time + (base_wait * (2 ** (retry_count - 1)))
```

Если все 3 retry исчерпаны — ошибка пробрасывается наверх.

## Уровень 3: Дневные квоты (RateLimiter)

Файл: `tganalytics/tganalytics/infra/limiter.py`

Синглтон `RateLimiter` ведёт **персистентные** суточные счётчики:

| Счётчик | Лимит | Что считает |
|---------|-------|------------|
| `dm_count` | 20/day | Отправленные DM |
| `join_count` | 20/day | Join/leave операции |
| `group_msg_count` | `MAX_GROUP_MSGS_PER_DAY` | Сообщения в группы/каналы |
| `api_calls` | без лимита | Общее число вызовов |
| `flood_waits` | алерт при >600с | Сколько раз TG заставил ждать |

Счётчики хранятся в `data/anti_spam/daily_counters.txt` (plain text `key=value`), обновляются атомарно (temp + rename), сбрасываются при смене даты. Если FLOOD_WAIT > 10 минут — логируется как `CRITICAL`.

Дополнительно доступен межпроцессный shared RPS бюджет (`TG_GLOBAL_RPS_MODE=shared`) через `data/anti_spam/global_rps_state.json` — это позволяет держать общий лимит при нескольких MCP-процессах.
Состояние circuit breaker синхронизируется через `data/anti_spam/flood_circuit_state.json`.

## Уровень 4: Smart Pause — паузы для bulk-операций

Файл: `tganalytics/tganalytics/infra/limiter.py`

Дополнительные паузы поверх rate limiter:

| Операция | Порог | Пауза |
|----------|-------|-------|
| `participants` | каждые 5000 записей | 1 сек |
| `dm_batch` | каждые 20 DM | 60 сек |
| `join_batch` | каждый join/leave | 3 сек |

Вызывается явно в бизнес-коде (например, `domain/groups.py` при экспорте участников).

## Уровень 5: Безопасность сессий

Файл: `tganalytics/tganalytics/infra/tele_client.py`

`.session` файлы Telethon — это SQLite с авторизацией. Утечка = полный доступ к аккаунту. Защита:

- **Каталог сессий** (`SESSION_DIR`) — `chmod 700` (только владелец)
- **Файлы сессий** — `chmod 600` (только владелец, только чтение/запись)
- `_harden_session_storage()` вызывается **при каждом создании клиента** и **после каждого disconnect** (на случай, если Telethon пересоздал файл)
- Путь к сессиям настраивается через `SESSION_DIR` env, по умолчанию `data/sessions/`

## Уровень 6: Блокировка direct write вне Action MCP

Файл: `tganalytics/tganalytics/infra/tele_client.py`

По умолчанию direct write-методы Telethon блокируются:
- `send_message`
- `send_file`
- `delete_messages`
- `edit_message`
- `forward_messages`

Также блокируются raw MTProto write-запросы через `client(Request)` (например `InviteToChannelRequest`, `DeleteChatUserRequest`, `EditBannedRequest`), если процесс не прошёл write policy.

Пишущие операции разрешаются только при одном из условий:
- процесс запущен в разрешённом write context (`TG_WRITE_CONTEXT` входит в `TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS`, обычно `actions_mcp`)
- либо явно включён debug override `TG_ALLOW_DIRECT_TELETHON_WRITE=1` (не использовать в production)
- в strict-режиме (`TG_ENFORCE_ACTION_PROCESS=1`) контекст `actions_mcp` работает только из процесса `mcp_server_actions.py`

Таким образом, нормальный рабочий контур записи в Telegram — через `tgmcp-actions` + `confirm=true` + allowlist.

Дополнительные контуры безопасности в `tgmcp-actions`:
- для non-dry-run write обязателен `confirmation_text` (явное подтверждение в текущем диалоге);
- `approval_code` после `dry_run` нельзя использовать мгновенно: действует минимальная выдержка `TG_ACTIONS_APPROVAL_MIN_AGE_SEC` (по умолчанию 30с) для human-review паузы;
- idempotency по хэшу action payload (chat+текст/файл/пользователь) блокирует дубли в окне 24ч;
- повтор по тем же параметрам возможен только с `force_resend=true`.
- для batch-задач approval не вечный: лизинг запуска ограничен `TG_ACTIONS_BATCH_APPROVAL_LEASE_SEC` (по умолчанию 24h), потом требуется re-approve.
- запуск одного и того же batch защищён run-lease (`TG_ACTIONS_BATCH_RUN_LEASE_SEC`), чтобы 2 worker-процесса не исполняли его одновременно.
- time-bounded write lane даёт один раз одобренное право только на `send_message` в явные allowlisted-чаты; scope включает TTL, общий и per-target лимит, минимальный интервал, длину сообщения и link policy.
- lane привязан к Telegram account ID в момент approval; смена active session блокирует отправку.
- lane нельзя расширить после preview: scope hash связывает approval с точным набором целей и ограничений. Расширение требует новой lane и нового approval.
- revoke сразу блокирует новые acquisition. Уже начатый Telegram request может завершиться; его результат сохраняется в metadata audit.
- `purpose` служит audit-контекстом и не является semantic content filter. Смысловую границу держит prompt внешнего monitor-agent; структурные ограничения enforce-ит сервер.
- fail-closed startup: если ослабить базовые safety-флаги (`allowlist/confirm/approval/idempotency/write-guard`), ActionMCP автоматически блокирует write (если не задан `TG_ACTIONS_UNSAFE_OVERRIDE=1`).
- state файлы ActionMCP (`action_approvals.json`, `action_idempotency.json`, `action_batches.json`, `action_lanes.json`) обновляются через file-lock + atomic replace, чтобы параллельные процессы не портили состояние.

## Enforcement: как это проверяется

### 1. Статический анализ

Файл: `scripts/check_anti_spam_compliance.py`

Сканирует все `.py` файлы на наличие «голых» вызовов к `client.*` без обёртки `safe_call`/`_safe_api_call`. Ищет паттерны типа:
- `await client.get_entity(...)` без safe_call
- `async for ... in client.iter_participants(...)` вне wrapper-функции

Умеет различать: вызов внутри wrapper-функции (допустимо) vs прямой вызов (нарушение). Может работать как pre-commit hook (принимает список файлов).

### 2. Invariants rule

Файл: `.cursor/rules/70-telegram-invariants.md`

Правило для AI-агентов:
- **MUST**: все Telethon-вызовы через `safe_call`, обязательный backoff, единый логгер
- **NEVER**: прямые Telethon-вызовы из проектного кода, ключи/ID в коде
- **SHOULD**: `.session` вне VCS, `.env.sample` без секретов

### 3. Адаптер для тестов

Файл: `tganalytics/tganalytics/domain/groups.py`

`_safe_api_call()` — детектирует тестовое окружение (pytest/unittest) и вызывает функцию напрямую, без rate limiter. Так моки работают без async-обёрток.

## Observability: метрики

Файл: `tganalytics/tganalytics/infra/metrics.py`

4 in-memory метрики с thread-safe доступом:

| Метрика | Тип | Что показывает |
|---------|-----|---------------|
| `rate_limit_requests_total` | counter | Сколько вызовов прошло через лимитер |
| `rate_limit_throttled_total` | counter | Сколько раз пришлось ждать токен |
| `flood_wait_events_total` | counter | Сколько раз TG вернул FLOOD_WAIT |
| `tele_call_latency_seconds` | histogram | Распределение задержек (бакеты: 50ms, 100ms, 250ms, 500ms, 1s, 2s, 5s, +Inf) |

`snapshot()` отдаёт текущее состояние всех метрик одним dict.

## Итого: цепочка гарантий

```
Проектный код → _safe_api_call → safe_call → TokenBucket → Telegram
                     ↓               ↓            ↓            ↓
              тест/прод адаптер   квоты DM/join  4 RPS      FLOOD_WAIT retry
                                                              ↓
                                                    exponential backoff
                                                    + метрики + алерт
```

Всё это ради одного: **не потерять Telegram-аккаунт** при любых bulk-операциях (экспорт участников, рассылка, join/leave).
