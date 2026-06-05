те# Обновленный .env.sample для Anti-Spam

Поскольку `.env.sample` защищен от редактирования, вот содержимое для обновления:

```bash
# Telegram API ключи
TG_API_ID=
TG_API_HASH=
SESSION_NAME=example_session

# Anti-spam настройки
RATE_RPS=4                    # Запросов в секунду
MAX_DM_PER_DAY=20            # Максимум DM в сутки
MAX_GROUPS=200               # Максимум групп для аккаунта
MAX_JOIN_LEAVE_PER_DAY=20    # Максимум join/leave в сутки
ACCOUNT_WARMUP_HOURS=24      # Часов прогрева для новых аккаунтов

# Flood-Wait настройки
MAX_FLOOD_WAIT_SECONDS=600   # Максимальное время ожидания
RETRY_BACKOFF_MULTIPLIER=1.5 # Множитель для экспоненциального backoff
MAX_RETRIES=3                # Максимальное количество повторов

# Мониторинг
ENABLE_SAFE_LOGGING=true     # Включить логирование anti-spam событий
SAFE_LOG_LEVEL=INFO          # Уровень логирования (DEBUG, INFO, WARNING, ERROR)
```

## Инструкция по обновлению

1. Скопировать содержимое выше
2. Заменить содержимое файла `.env.sample`
3. Обновить свой `.env` файл с нужными значениями

## Описание новых параметров

### Anti-spam настройки
- **RATE_RPS**: Ограничение скорости запросов к Telegram API (рекомендуется 4)
- **MAX_DM_PER_DAY**: Максимальное количество личных сообщений в сутки
- **MAX_GROUPS**: Максимальное количество групп для аккаунта
- **MAX_JOIN_LEAVE_PER_DAY**: Максимальное количество операций входа/выхода из групп в сутки
- **ACCOUNT_WARMUP_HOURS**: Время прогрева для новых аккаунтов

### Flood-Wait настройки
- **MAX_FLOOD_WAIT_SECONDS**: Максимальное время ожидания при получении FLOOD_WAIT
- **RETRY_BACKOFF_MULTIPLIER**: Множитель для экспоненциального увеличения задержки
- **MAX_RETRIES**: Максимальное количество повторных попыток

### Мониторинг
- **ENABLE_SAFE_LOGGING**: Включить/выключить логирование anti-spam событий
- **SAFE_LOG_LEVEL**: Уровень детализации логов 