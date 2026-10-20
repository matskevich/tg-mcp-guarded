# Антиспам и безопасность в tganalytics

## Зачем это нужно

антиспам и ограничение частоты — разные механизмы. `FLOOD_WAIT` требует выждать указанное сервером время и сам по себе не означает спам-бан. жалобы на нежелательные сообщения могут приводить к ограничениям аккаунта. локальные дневные квоты ниже — наши операционные предохранители, не опубликованные telegram пороги безопасности. все вызовы проходят rate limiter.

источники, проверены 2026-10-20: [telegram spam faq](https://telegram.org/faq_spam), [api errors](https://core.telegram.org/api/errors). telegram отдельно допускает ответы на входящие; это не отменяет серверные ограничения скорости.

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


## политика ответов, 2026-10-20

цель — ограничивать нежелательные обращения и ошибочные циклы, не останавливая порученный ответ из-за бюджета массовых исходящих. полномочие владельца и ожидаемость сообщения — разные проверки: наличие входящего не даёт агенту права самостоятельно общаться без задачи или действующего lane.

`send_message` проверяет последние 20 сообщений личного диалога через штатный API wrapper. отдельный класс `conversation_reply` доступен только для человека (не бота, группы или собственного аккаунта), если сервер вернул входящее от этого же человека не старше 7 дней. вызывающий не может передать флаг «это ответ». при ошибке чтения отправка не происходит; при отсутствии доказательства действует прежний `group_msg` бюджет.

начальные операционные значения: 100 попыток ответа в сутки на runtime, 20 на собеседника, минимум 10 секунд между попытками одному собеседнику. это гипотеза о достаточном рабочем запасе и защита от зацикливания, не обещание безопасности от telegram. существующие исходящие, групповые сообщения, файлы, пересылки и боты остаются в прежнем бюджете 30; их классификация — отдельное последующее изменение. join/dm бюджеты не увеличены.

резервирование происходит атомарно под межпроцессной блокировкой до отправки; состояние общее в `data/anti_spam/conversation_reply_budget.json`. сохраняются только хеш идентификатора собеседника и счётчики, без текста. дата сброса — 00:00 UTC. неопределённый исход расходует попытку; автоматического возврата или повторной отправки нет. повреждённое состояние блокирует работу, а не обнуляет лимит. исчерпание reply-бюджета не переключает отправку в запасной класс.

shared RPS, `FLOOD_WAIT`, circuit breaker, account binding, allowlist, approval-code, idempotency и lane quotas сохраняются. реальный отказ telegram нельзя обходить сменой процесса, аккаунта, класса или квоты. локальное исчерпание возвращается как `local_quota_exceeded`, отдельно от общего transport failure. в lanes текст локальной ошибки также сохраняется.

область состояния: один аккаунт на каталог anti_spam, как и у прежнего limiter. разные аккаунты должны иметь разные каталоги. старые уже запущенные процессы используют прежний код до перезапуска; не смешивать их счётчики с доказательством применения новой политики.

оценка через неделю: число заблокированных полезных ответов, количество собеседников, повторы одному адресату, `FLOOD_WAIT` и неизвестные результаты. 100/20/10 корректируются по этим данным и поручению владельца, не вслед за каждой ошибкой отправки. автоматическая адаптация и мониторинг не включены. для отката установить `MAX_CONVERSATION_REPLIES_PER_DAY=0` в новых процессах либо вернуть предыдущую версию кода; состояние и квоты не стирать.
