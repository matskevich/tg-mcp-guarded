# Anti-Spam План для tg-mcp

## 🎯 Цель
Защита от блокировок Telegram через интеллектуальное управление скоростью запросов и соблюдение лимитов API.

## 📐 Базовый принцип
**"Не считай минуты — считай RPC-токены"** → держим 4 запроса/сек и ловим Flood-Wait сервером, а не баном.

## 🏗️ Архитектура решения

```
src/infra/
├── limiter.py          # Центральный rate limiter
├── tele_client.py      # Обновленный клиент с safe_call
└── monitors.py         # Мониторинг и алерты

src/core/
└── group_manager.py    # Интеграция с rate limiter

scripts/
└── security_check.py  # Расширенная проверка с anti-spam метриками
```

## 1️⃣ Rate Limiter (src/infra/limiter.py)

### Компоненты:
- **Token Bucket** алгоритм для 4 RPS
- **safe_call** wrapper для всех Telegram API вызовов
- **FLOOD_WAIT** автоматический retry с экспоненциальным backoff
- **Глобальные счетчики** для DM, join/leave операций

### Основные функции:
```python
async def safe_call(coro, ctx="unknown", max_retries=3):
    """
    Безопасный вызов Telegram API с rate limiting и retry
    
    Args:
        coro: Корутина для вызова (например, client.get_entity())
        ctx: Контекст вызова для логирования
        max_retries: Максимальное количество повторных попыток
    """

class RateLimiter:
    """
    Token bucket rate limiter с глобальными квотами
    """
    def __init__(self, rps=4, max_dm_per_day=20, max_groups=200):
        pass
        
    async def acquire(self, tokens=1):
        """Получить разрешение на выполнение запроса"""
        
    def check_dm_quota(self):
        """Проверить квоту DM за сутки"""
        
    def increment_dm_count(self):
        """Увеличить счетчик DM"""
```

## 2️⃣ Квоты по операциям

### Лимиты:
- **≤ 200 чатов** где аккаунт в админах/участниках
- **≤ 20 DM в сутки** с ежедневным сбросом в 00:00 UTC
- **≤ 20 join/leave** операций в сутки
- **24ч прогрев** для новых SIM-карт (блокировка скриптов)

### Счетчики:
```python
{
    "dm_count_today": 0,
    "last_dm_reset": "2024-01-01",
    "join_leave_count_today": 0,
    "groups_count": 0,
    "account_age_hours": 168  # 7 дней в часах
}
```

## 3️⃣ Интеллектуальные паузы

### Стратегии:
- **Каждые 5000 участников** → `await asyncio.sleep(1)`
- **Длинные рассылки** делить на пачки по 20 DM, пауза 60 сек между пачками
- **Adaptive backoff** при получении FLOOD_WAIT
- **Randomized delays** для имитации человеческого поведения

### Реализация:
```python
async def smart_pause(operation_type, count):
    """
    Интеллектуальная пауза в зависимости от типа операции
    
    Args:
        operation_type: "fetch_members", "send_dm", "join_group"
        count: Количество выполненных операций
    """
    if operation_type == "fetch_members" and count % 5000 == 0:
        await asyncio.sleep(1)
    elif operation_type == "send_dm" and count % 20 == 0:
        await asyncio.sleep(60)
```

## 4️⃣ Конфигурация в .env

### Новые параметры:
```bash
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

## 5️⃣ Мониторинг и алерты

### Расширение security_check.py:
```python
def check_anti_spam_metrics():
    """
    Проверка anti-spam метрик
    
    Returns:
        dict: Словарь с метриками
    """
    return {
        "dm_count_today": limiter.dm_count_today,
        "rps_average_24h": calculate_rps_average(),
        "flood_waits_over_600s": count_long_flood_waits(),
        "groups_count": count_user_groups(),
        "account_age_days": get_account_age_days(),
        "last_rate_limit": get_last_rate_limit_time()
    }
```

### Memory log с тегом SAFE:
```
[2024-01-15 10:30:00] SAFE: RPS average: 3.2/sec (OK)
[2024-01-15 10:30:00] SAFE: DM count today: 15/20 (OK)
[2024-01-15 10:30:00] SAFE: Groups count: 150/200 (OK)
[2024-01-15 10:30:00] SAFE: Long flood waits: 0 (OK)
[2024-01-15 10:30:00] SAFE: Account age: 7 days (OK)
```

## 6️⃣ Аккаунт-гигиена

### Требования:
- **Уникальное имя**, отличное от номера телефона
- **Аватар** загружен и настроен
- **Биография** заполнена (не пустая)
- **2FA включена** для дополнительной безопасности
- **Отдельный номер** под скрапинг (не основной)

### Проверка:
```python
async def check_account_hygiene(client):
    """
    Проверка гигиены аккаунта
    
    Returns:
        dict: Результаты проверок
    """
    me = await client.get_me()
    return {
        "has_username": bool(me.username),
        "has_first_name": bool(me.first_name),
        "has_last_name": bool(me.last_name),
        "has_photo": bool(me.photo),
        "has_bio": bool(me.about),
        "has_2fa": await check_2fa_enabled(client)
    }
```

## 7️⃣ Интеграция с существующим кодом

### Обновление tele_client.py:
```python
from tganalytics.infra.limiter import safe_call, rate_limiter

async def test_connection():
    """Тестирует подключение к Telegram API с rate limiting"""
    try:
        client = get_client()
        await client.start()
        
        # Используем safe_call для API вызовов
        me = await safe_call(client.get_me(), ctx="test_connection")
        print(f"✅ Подключение успешно: {me.username} (ID: {me.id})")
        
        await client.disconnect()
        return True
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        return False
```

### Обновление group_manager.py:
```python
from tganalytics.infra.limiter import safe_call, smart_pause

async def get_participants(self, group_identifier: str, limit: int = 100):
    """Получает список участников с rate limiting"""
    participants = []
    count = 0
    
    try:
        # Получаем информацию о группе с rate limiting
        group_info = await safe_call(
            self.get_group_info(group_identifier), 
            ctx="get_group_info"
        )
        
        # Получаем участников с интеллектуальными паузами
        async for user in self.client.iter_participants(group_id, limit=limit):
            if isinstance(user, User) and not user.bot:
                participant_info = {
                    # ... существующий код ...
                }
                participants.append(participant_info)
                count += 1
                
                # Интеллектуальная пауза каждые 5000 участников
                await smart_pause("fetch_members", count)
        
        return participants
    except Exception as e:
        logger.error(f"Ошибка при получении участников: {e}")
        return []
```

## 8️⃣ Unit тесты

### Тестирование rate limiter:
```python
async def test_rate_limiter_throttling():
    """Тест: 10 throttles @5rps должно занять ≥2 секунды"""
    limiter = RateLimiter(rps=5)
    
    start_time = time.time()
    
    # Выполняем 10 запросов
    for i in range(10):
        await limiter.acquire()
    
    end_time = time.time()
    elapsed = end_time - start_time
    
    # 10 запросов при 5 RPS должно занять минимум 2 секунды
    assert elapsed >= 2.0, f"Expected ≥2s, got {elapsed}s"
```

### Тестирование safe_call:
```python
async def test_safe_call_flood_wait_retry():
    """Тест автоматического retry при FLOOD_WAIT"""
    mock_client = AsyncMock()
    
    # Настраиваем мок для имитации FLOOD_WAIT, затем успех
    mock_client.get_entity.side_effect = [
        FloodWaitError(5),  # Первый вызов - ошибка
        mock_user           # Второй вызов - успех
    ]
    
    result = await safe_call(
        mock_client.get_entity("test"), 
        ctx="test_flood_wait"
    )
    
    assert result == mock_user
    assert mock_client.get_entity.call_count == 2
```

## 9️⃣ План реализации

### Этап 1: Базовый Rate Limiter (1-2 дня)
1. Создать `src/infra/limiter.py` с token bucket
2. Реализовать `safe_call` wrapper
3. Добавить базовые unit тесты
4. Обновить `.env.sample`

### Этап 2: Интеграция (1 день)
1. Обновить `tele_client.py` для использования `safe_call`
2. Обновить `group_manager.py` с интеллектуальными паузами
3. Тестирование интеграции

### Этап 3: Квоты и мониторинг (1-2 дня)
1. Добавить счетчики DM и join/leave операций
2. Расширить `security_check.py` с anti-spam метриками
3. Добавить memory logging с тегом SAFE
4. Проверка аккаунт-гигиены

### Этап 4: Тестирование и оптимизация (1 день)
1. Comprehensive unit и integration тесты
2. Тестирование на реальных данных
3. Оптимизация параметров
4. Документация

## 🔧 Технические детали

### Token Bucket алгоритм:
```python
import asyncio
import time
from typing import Optional

class TokenBucket:
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.time()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> bool:
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            
            # Добавляем токены на основе времени
            self.tokens = min(
                self.capacity, 
                self.tokens + elapsed * self.refill_rate
            )
            self.last_refill = now
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            else:
                # Ждем пока появятся токены
                wait_time = (tokens - self.tokens) / self.refill_rate
                await asyncio.sleep(wait_time)
                self.tokens = max(0, self.tokens - tokens)
                return True
```

### FLOOD_WAIT обработка:
```python
async def handle_flood_wait(seconds: int, ctx: str) -> None:
    """Обработка FLOOD_WAIT с логированием"""
    if seconds > 600:  # 10 минут
        logger.warning(f"SAFE: Long FLOOD_WAIT {seconds}s in {ctx}")
    
    logger.info(f"SAFE: Waiting {seconds}s due to FLOOD_WAIT in {ctx}")
    await asyncio.sleep(seconds + 1)  # +1 секунда для безопасности
```

## ⚠️ Предупреждения

1. **Не снижать лимиты** ниже рекомендованных значений
2. **Не игнорировать FLOOD_WAIT** - всегда ждать указанное время
3. **Мониторить алерты** в memory log с тегом SAFE
4. **Использовать отдельные аккаунты** для скрапинга
5. **Регулярно проверять** аккаунт-гигиену

## 📈 Ожидаемые результаты

- **Снижение риска блокировки** на 90%+
- **Стабильная работа** при больших объемах данных
- **Автоматическое восстановление** после временных ограничений
- **Детальный мониторинг** использования API
- **Соблюдение best practices** Telegram API 