# CLAUDE.md — контекст для AI-агентов

Этот файл — точка входа для Claude Code, Cline, Cursor и других AI-агентов.

## Что это за репозиторий

**tg-mcp** — MCP-сервер + Python-библиотека для работы с Telegram API.

Предоставляет:
- Telegram-клиенты с управлением сессиями
- Rate limiting и anti-spam защита (TokenBucket, safe_call, дневные квоты)
- Экспорт данных (участники, сообщения, группы)
- MCP-сервер для доступа из Claude Code

## Структура

```
/
├── tganalytics/            # Telegram-инфраструктура
│   ├── tganalytics/        # пакет (infra, domain, config)
│   ├── mcp_server.py       # MCP-сервер (9 tools)
│   ├── examples/           # примеры использования
│   └── pyproject.toml
├── tests/                  # тесты
├── scripts/                # утилиты (compliance, security)
├── docs/                   # документация
├── data/                   # runtime данные (gitignored)
│   ├── sessions/           # Telegram-сессии
│   ├── anti_spam/          # дневные счётчики
│   └── logs/
├── .cursor/rules/          # AI governance
├── .github/workflows/      # CI
├── .mcp.json               # MCP конфигурация
└── requirements.txt
```

## Ключевые правила

1. **Все Telegram-вызовы через safe_call** — никаких прямых telethon вызовов
2. **PII не коммитить** — все выгрузки в gitignored папках
3. **Session-файлы защищены** — chmod 700/600, не коммитить
4. **Telegram write only via Action MCP** — direct `client.send_*` и `client.delete_messages` запрещены по умолчанию

## MCP Server

Read/Actions MCP tools для доступа к Telegram API:

| Tool | Назначение |
|------|------------|
| `tg_list_sessions` | Список сессий |
| `tg_use_session` | Переключить сессию |
| `tg_get_group_info` | Инфо о группе |
| `tg_get_participants` | Участники группы |
| `tg_search_participants` | Поиск участников |
| `tg_get_messages` | Сообщения |
| `tg_get_message_count` | Количество сообщений |
| `tg_get_group_creation_date` | Дата создания группы |
| `tg_get_stats` | Статистика anti-spam |
| `tg_send_message` | Отправка сообщения (actions profile: dry_run -> approval_code -> confirm=true + confirmation_text) |
| `tg_send_file` | Отправка файла (actions profile: dry_run -> approval_code -> confirm=true + confirmation_text) |
| `tg_get_actions_policy` | Активные write-ограничения |

## Использование из другого проекта

Предпочтительно подключать split-профили, а не legacy alias `telegram`/`tganalytics`.

Добавь в `.mcp.json`:
```json
{
  "mcpServers": {
    "tgmcp-read": {
      "command": "/absolute/path/to/tg-mcp/venv/bin/python3",
      "args": [
        "/absolute/path/to/tg-mcp/tganalytics/mcp_server_read.py"
      ],
      "env": {
        "PYTHONPATH": "/absolute/path/to/tg-mcp/tganalytics:/absolute/path/to/tg-mcp",
        "TG_SESSIONS_DIR": "/absolute/path/to/tg-mcp/data/sessions",
        "TG_SESSION_PATH": "/absolute/path/to/tg-mcp/data/sessions/my_account_ro.session",
        "TG_ALLOW_SESSION_SWITCH": "0",
        "TG_BLOCK_DIRECT_TELETHON_WRITE": "1",
        "TG_ALLOW_DIRECT_TELETHON_WRITE": "0",
        "TG_ENFORCE_ACTION_PROCESS": "1",
        "TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS": "actions_mcp",
        "TG_WRITE_CONTEXT": "read_mcp",
        "TG_ACTION_PROCESS": "0",
        "TG_RECEIVE_UPDATES": "0",
        "TG_SESSION_LOCK_MODE": "shared",
        "TG_GLOBAL_RPS_MODE": "shared",
        "TG_FLOOD_CIRCUIT_THRESHOLD_SEC": "300",
        "TG_FLOOD_CIRCUIT_COOLDOWN_SEC": "900",
        "TG_EXPECTED_USERNAME": "my_account"
      }
    },
    "tgmcp-actions": {
      "command": "/absolute/path/to/tg-mcp/venv/bin/python3",
      "args": [
        "/absolute/path/to/tg-mcp/tganalytics/mcp_server_actions.py"
      ],
      "env": {
        "PYTHONPATH": "/absolute/path/to/tg-mcp/tganalytics:/absolute/path/to/tg-mcp",
        "TG_SESSIONS_DIR": "/absolute/path/to/tg-mcp/data/sessions",
        "TG_SESSION_PATH": "/absolute/path/to/tg-mcp/data/sessions/my_account.session",
        "TG_ALLOW_SESSION_SWITCH": "0",
        "TG_ACTIONS_ENABLED": "1",
        "TG_ACTIONS_REQUIRE_ALLOWLIST": "1",
        "TG_ACTIONS_ALLOWED_GROUPS": "@your_allowed_target",
        "TG_ACTIONS_MAX_MESSAGE_LEN": "2000",
        "TG_ACTIONS_MAX_FILE_MB": "20",
        "TG_ACTIONS_REQUIRE_CONFIRMATION_TEXT": "1",
        "TG_ACTIONS_CONFIRMATION_PHRASE": "",
        "TG_ACTIONS_MIN_CONFIRM_TEXT_LEN": "6",
        "TG_ACTIONS_REQUIRE_APPROVAL_CODE": "1",
        "TG_ACTIONS_APPROVAL_TTL_SEC": "1800",
        "TG_ACTIONS_APPROVAL_MIN_AGE_SEC": "30",
        "TG_ACTIONS_APPROVAL_FILE": "/absolute/path/to/tg-mcp/data/anti_spam/action_approvals.json",
        "TG_ACTIONS_IDEMPOTENCY_ENABLED": "1",
        "TG_ACTIONS_IDEMPOTENCY_WINDOW_SEC": "86400",
        "TG_ACTIONS_IDEMPOTENCY_FILE": "/absolute/path/to/tg-mcp/data/anti_spam/action_idempotency.json",
        "TG_ACTIONS_BATCH_FILE": "/absolute/path/to/tg-mcp/data/anti_spam/action_batches.json",
        "TG_ACTIONS_BATCH_TTL_HOURS": "168",
        "TG_ACTIONS_BATCH_APPROVAL_LEASE_SEC": "86400",
        "TG_ACTIONS_BATCH_RUN_LEASE_SEC": "1800",
        "TG_ACTIONS_UNSAFE_OVERRIDE": "0",
        "TG_BLOCK_DIRECT_TELETHON_WRITE": "1",
        "TG_ALLOW_DIRECT_TELETHON_WRITE": "0",
        "TG_ENFORCE_ACTION_PROCESS": "1",
        "TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS": "actions_mcp",
        "TG_WRITE_CONTEXT": "actions_mcp",
        "TG_ACTION_PROCESS": "1",
        "TG_RECEIVE_UPDATES": "0",
        "TG_SESSION_LOCK_MODE": "shared",
        "TG_GLOBAL_RPS_MODE": "shared",
        "TG_FLOOD_CIRCUIT_THRESHOLD_SEC": "300",
        "TG_FLOOD_CIRCUIT_COOLDOWN_SEC": "900",
        "MAX_GROUP_MSGS_PER_DAY": "30",
        "TG_EXPECTED_USERNAME": "my_account"
      }
    }
  }
}
```

Если используешь claude-code, в `~/.claude/settings.local.json` или project `.claude/settings.local.json` включай именно:
```json
{
  "enableAllProjectMcpServers": true,
  "enabledMcpjsonServers": ["tgmcp-read", "tgmcp-actions"],
  "disabledMcpjsonServers": ["telegram"]
}
```

Иначе claude может подключить legacy alias `telegram` и увидеть только read-capability.

## Команды

```bash
# тесты
PYTHONPATH=tganalytics:. python3 -m pytest tests/ -q

# MCP server (ручной запуск для отладки)
PYTHONPATH=tganalytics:. venv/bin/python3 tganalytics/mcp_server.py

# проверка anti-spam compliance
python3 scripts/check_anti_spam_compliance.py
```

## Документация

- `docs/ANTISPAM_SECURITY.md` — архитектура антиспам-системы
- `docs/ANTISPAM_IMPROVEMENTS_PLAN.md` — план улучшений
- `.cursor/rules/70-telegram-invariants.md` — обязательные правила
