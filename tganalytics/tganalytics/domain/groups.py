import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.errors.rpcerrorlist import (
    UserAlreadyParticipantError,
    UserNotParticipantError,
)
from telethon.tl.types import Channel, Chat, User

from tganalytics.infra.limiter import safe_call, smart_pause

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _validate_group_identifier(group_identifier: str) -> tuple[bool, str]:
    """
    Валидирует идентификатор группы.

    Returns:
        (is_valid, error_message)
    """
    if not group_identifier:
        return False, "Group identifier is empty"

    # Если это числовой ID — всегда валидно
    if isinstance(group_identifier, int):
        return True, ""

    # Строковый числовой ID (например "-1001234567890")
    if isinstance(group_identifier, str):
        stripped = group_identifier.lstrip("-")
        if stripped.isdigit():
            return True, ""

        # Username не может содержать пробелы
        if " " in group_identifier:
            return (
                False,
                f"Invalid group identifier '{group_identifier}': contains spaces. "
                "Use numeric ID or valid username without spaces.",
            )

    # Username должен быть 5-32 символа, только a-z, 0-9, _
    clean_username = group_identifier.lstrip("@")
    if len(clean_username) < 5:
        return (
            False,
            f"Invalid username '{group_identifier}': too short (min 5 characters)",
        )
    if len(clean_username) > 32:
        return (
            False,
            f"Invalid username '{group_identifier}': too long (max 32 characters)",
        )

    # Проверяем допустимые символы
    import re

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", clean_username):
        return (
            False,
            f"Invalid username '{group_identifier}': must start with letter "
            "and contain only a-z, 0-9, _",
        )

    return True, ""


# Проверяем тестовое окружение
def _is_testing_environment():
    """Определяет тестовое окружение"""
    import sys

    return os.getenv("TG_DISABLE_RATE_LIMIT_FOR_TESTS", "1") == "1" and (
        "pytest" in sys.modules
        or "unittest" in sys.modules
        or os.getenv("PYTEST_CURRENT_TEST") is not None
    )


async def _safe_api_call(func, *args, operation_type: str = "api", **kwargs):
    """Helper для условного использования safe_call в зависимости от окружения"""
    if _is_testing_environment():
        # В тестах используем прямые вызовы для совместимости с моками
        logger.debug(
            f"[TEST] Calling {func.__name__ if hasattr(func, '__name__') else 'function'} directly"
        )
        return await func(*args, **kwargs)
    else:
        # В продакшене используем safe_call для анти-спам защиты
        func_name = func.__name__ if hasattr(func, "__name__") else "function"
        logger.debug(f"[PROD] Calling {func_name} via safe_call")
        return await safe_call(func, operation_type=operation_type, *args, **kwargs)


class GroupManager:
    """Менеджер для работы с группами Telegram"""

    def __init__(self, client: TelegramClient):
        self.client = client

    @staticmethod
    def _dialog_title(entity: Union[Channel, Chat, User]) -> str:
        """Нормализованное отображаемое имя диалога для group/user targets."""
        if isinstance(entity, (Channel, Chat)):
            return getattr(entity, "title", "") or ""

        full_name = " ".join(
            part.strip()
            for part in [
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            ]
            if part
        ).strip()
        return (
            full_name
            or getattr(entity, "username", None)
            or str(getattr(entity, "id", ""))
        )

    async def _resolve_read_entity(
        self,
        group_identifier: Union[str, int],
        allow_users: bool = False,
    ) -> Optional[Union[Channel, Chat, User]]:
        """Resolve a readable dialog target for history/info operations.

        Supports numeric IDs, usernames, and exact dialog titles with spaces.
        """
        is_valid, error_msg = _validate_group_identifier(group_identifier)
        if not is_valid:
            if isinstance(group_identifier, str) and " " in group_identifier:
                by_title = await self._resolve_dialog_entity_by_title(
                    group_identifier,
                    allow_users=allow_users,
                )
                if by_title:
                    return by_title
            logger.error(f"Validation failed: {error_msg}")
            return None

        try:
            if isinstance(group_identifier, int):
                entity_id: Union[str, int] = group_identifier
            elif isinstance(group_identifier, str) and (
                group_identifier.startswith("-") and group_identifier[1:].isdigit()
            ):
                entity_id = int(group_identifier)
            else:
                entity_id = (
                    group_identifier
                    if group_identifier.startswith("@")
                    else "@" + group_identifier
                )

            entity = await _safe_api_call(self.client.get_entity, entity_id)
            if isinstance(entity, (Channel, Chat)) or (
                allow_users and isinstance(entity, User)
            ):
                return entity
        except Exception as e:
            logger.error(f"Ошибка при разрешении группы {group_identifier}: {e}")

        return None

    async def _resolve_group_entity(
        self, group_identifier: Union[str, int]
    ) -> Optional[Union[Channel, Chat]]:
        """Resolve a group/channel identifier for group-only operations."""
        entity = await self._resolve_read_entity(group_identifier, allow_users=False)
        if isinstance(entity, (Channel, Chat)):
            return entity
        return None

    async def _group_info_from_entity(
        self, entity: Union[Channel, Chat]
    ) -> Dict[str, Any]:
        """Build normalized group info payload from Telegram entity."""
        participants_count = getattr(entity, "participants_count", None)

        # Если participants_count отсутствует или равен 0, пытаемся получить более точное число
        if participants_count is None or participants_count == 0:
            try:
                # Для публичных каналов/групп пытаемся получить full info
                async def get_full_info():
                    if isinstance(entity, Channel):
                        from telethon.tl.functions.channels import GetFullChannelRequest

                        full_info = await _safe_api_call(
                            self.client,
                            GetFullChannelRequest(entity),
                            operation_type="api",
                        )
                        return getattr(full_info.full_chat, "participants_count", None)

                    from telethon.tl.functions.messages import GetFullChatRequest

                    full_info = await _safe_api_call(
                        self.client,
                        GetFullChatRequest(entity.id),
                        operation_type="api",
                    )
                    return getattr(full_info.full_chat, "participants_count", None)

                full_participants_count = await _safe_api_call(get_full_info)
                if full_participants_count is not None:
                    participants_count = full_participants_count
            except Exception as e:
                logger.debug(
                    f"Не удалось получить полную информацию о группе {entity.id}: {e}"
                )

        return {
            "id": entity.id,
            "title": entity.title,
            "username": getattr(entity, "username", None),
            "participants_count": participants_count,
            "type": "channel" if isinstance(entity, Channel) else "group",
        }

    async def _read_info_from_entity(
        self, entity: Union[Channel, Chat, User]
    ) -> Dict[str, Any]:
        """Build normalized read/info payload for groups, channels, and direct dialogs."""
        if isinstance(entity, User):
            return {
                "id": entity.id,
                "title": self._dialog_title(entity),
                "username": entity.username,
                "participants_count": None,
                "type": "user",
                "first_name": entity.first_name,
                "last_name": entity.last_name,
                "is_bot": entity.bot,
                "is_premium": getattr(entity, "premium", False),
            }
        return await self._group_info_from_entity(entity)

    async def _resolve_dialog_entity_by_title(
        self,
        group_title: str,
        allow_users: bool = False,
    ) -> Optional[Union[Channel, Chat, User]]:
        """
        Resolve a dialog by exact title (case-insensitive).

        Useful for UX where users provide "chat title" instead of @username/ID.
        """
        target = str(group_title or "").strip().lower()
        if not target:
            return None

        allowed_types = (Channel, Chat, User) if allow_users else (Channel, Chat)

        async def iter_dialogs():
            async for dialog in self.client.iter_dialogs(limit=300):
                entity = getattr(dialog, "entity", None)
                title = (
                    (
                        getattr(dialog, "title", None)
                        or getattr(dialog, "name", None)
                        or (self._dialog_title(entity) if entity else None)
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if title == target and isinstance(entity, allowed_types):
                    return entity
            return None

        try:
            return await _safe_api_call(iter_dialogs)
        except Exception as e:
            logger.debug(f"Не удалось резолвить группу по title '{group_title}': {e}")
            return None

    async def get_group_info(self, group_identifier: str) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о группе

        Args:
            group_identifier: username группы (без @) или ID группы

        Returns:
            Словарь с информацией о группе или None
        """
        entity = await self._resolve_read_entity(group_identifier, allow_users=True)
        if entity:
            return await self._read_info_from_entity(entity)
        return None

    async def get_participants(
        self, group_identifier: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Получает список участников группы

        Args:
            group_identifier: username группы (без @) или ID группы
            limit: максимальное количество участников для получения

        Returns:
            Список словарей с информацией об участниках
        """
        participants = []

        try:
            entity = await self._resolve_group_entity(group_identifier)
            if not entity:
                logger.error(f"Не удалось найти группу: {group_identifier}")
                return []

            group_info = await self._group_info_from_entity(entity)
            logger.info(f"Получаем участников группы: {group_info['title']}")

            # Получаем участников с anti-spam защитой через safe_call
            count = 0

            # Создаем wrapper функцию для безопасного получения участников
            async def get_participants_safe():
                users = []
                async for user in self.client.iter_participants(entity, limit=limit):
                    users.append(user)
                return users

            # Вызываем через safe_call для анти-спам защиты
            users = await _safe_api_call(get_participants_safe)

            for user in users:
                if isinstance(user, User) and not user.bot:  # Исключаем ботов
                    participant_info = {
                        "id": user.id,
                        "username": user.username,
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "phone": user.phone,
                        "is_bot": user.bot,
                        "is_verified": user.verified,
                        "is_premium": getattr(user, "premium", False),
                        "status": str(user.status) if user.status else None,
                    }
                    participants.append(participant_info)
                    count += 1

                    # Smart pause каждые 1000 участников для предотвращения FLOOD_WAIT
                    if count % 1000 == 0:
                        await smart_pause("participants", count)

            logger.info(
                f"Получено {len(participants)} участников из группы {group_info['title']}"
            )
            return participants

        except ChatAdminRequiredError:
            logger.error(
                f"Нет прав администратора для получения участников группы: {group_identifier}"
            )
            return []
        except FloodWaitError as e:
            logger.error(f"Превышен лимит запросов. Ожидание {e.seconds} секунд")
            return []
        except Exception as e:
            logger.error(
                f"Ошибка при получении участников группы {group_identifier}: {e}"
            )
            return []

    async def search_participants(
        self, group_identifier: str, query: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Ищет участников в группе по запросу

        Args:
            group_identifier: username группы (без @) или ID группы
            query: поисковый запрос
            limit: максимальное количество результатов

        Returns:
            Список найденных участников
        """
        participants = []

        try:
            logger.info(
                f"Поиск участников в группе {group_identifier} по запросу: {query}"
            )

            entity = await self._resolve_group_entity(group_identifier)
            if not entity:
                logger.error(f"Не удалось найти группу: {group_identifier}")
                return []

            # Создаем wrapper функцию для безопасного поиска участников
            async def search_participants_safe():
                users = []
                async for user in self.client.iter_participants(
                    entity, search=query, limit=limit
                ):
                    users.append(user)
                return users

            # Вызываем через safe_call для анти-спам защиты
            users = await _safe_api_call(search_participants_safe)

            for user in users:
                if isinstance(user, User) and not user.bot:
                    participant_info = {
                        "id": user.id,
                        "username": user.username,
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "phone": user.phone,
                        "is_bot": user.bot,
                        "is_verified": user.verified,
                        "is_premium": getattr(user, "premium", False),
                        "status": str(user.status) if user.status else None,
                    }
                    participants.append(participant_info)

            logger.info(f"Найдено {len(participants)} участников по запросу '{query}'")
            return participants

        except Exception as e:
            logger.error(f"Ошибка при поиске участников: {e}")
            return []

    async def export_participants_to_csv(
        self, group_identifier: str, filename: str, limit: int = 1000
    ) -> bool:
        """
        Экспортирует список участников в CSV файл

        Args:
            group_identifier: username группы (без @) или ID группы
            filename: имя файла для сохранения
            limit: максимальное количество участников

        Returns:
            True если экспорт успешен, False в противном случае
        """
        import csv

        try:
            participants = await self.get_participants(group_identifier, limit)

            if not participants:
                logger.warning("Нет участников для экспорта")
                return False

            with open(filename, "w", newline="", encoding="utf-8") as csvfile:
                fieldnames = [
                    "id",
                    "username",
                    "first_name",
                    "last_name",
                    "phone",
                    "is_verified",
                    "is_premium",
                    "status",
                ]
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

                writer.writeheader()
                for participant in participants:
                    # Очищаем данные для CSV
                    clean_participant = {
                        k: v for k, v in participant.items() if k in fieldnames
                    }
                    writer.writerow(clean_participant)

            logger.info(
                f"Экспортировано {len(participants)} участников в файл {filename}"
            )
            return True

        except Exception as e:
            logger.error(f"Ошибка при экспорте в CSV: {e}")
            return False

    async def get_group_creation_date(
        self, group_identifier: Union[str, int]
    ) -> Optional[datetime]:
        """
        Получает приблизительную дату создания группы через первое сообщение

        Использует быстрый метод: iter_messages(reverse=True, limit=1)
        Всего 1 API вызов даже для групп с миллионами сообщений

        Args:
            group_identifier: username группы (без @) или ID группы

        Returns:
            datetime объект с датой создания или None при ошибке
        """
        try:
            entity = await self._resolve_read_entity(group_identifier, allow_users=True)
            if not entity:
                logger.error(f"Не удалось найти группу: {group_identifier}")
                return None

            # Функция для получения первого сообщения
            async def get_first_message():
                async for msg in self.client.iter_messages(
                    entity, reverse=True, limit=1
                ):
                    return msg.date
                return None

            # Вызываем через safe_call для анти-спам защиты
            creation_date = await _safe_api_call(get_first_message)

            if creation_date:
                logger.info(
                    f"Получена дата создания группы {group_identifier}: {creation_date}"
                )
                return creation_date
            else:
                logger.warning(
                    f"Не удалось получить дату создания для группы {group_identifier}"
                )
                return None

        except Exception as e:
            logger.error(
                f"Ошибка при получении даты создания группы {group_identifier}: {e}"
            )
            return None

    async def get_message_count(
        self, group_identifier: Union[str, int]
    ) -> Optional[int]:
        """
        Быстро получает количество сообщений в чате/группе (если доступно).

        Использует `messages.GetHistoryRequest(limit=0)`, что обычно возвращает `count`
        без выгрузки истории.
        """
        try:
            entity = await self._resolve_read_entity(group_identifier, allow_users=True)
            if not entity:
                logger.error(f"Не удалось найти группу: {group_identifier}")
                return None

            async def get_count():
                from telethon.tl.functions.messages import GetHistoryRequest

                hist = await _safe_api_call(
                    self.client,
                    GetHistoryRequest(
                        peer=entity,
                        limit=0,
                        offset_date=None,
                        offset_id=0,
                        max_id=0,
                        min_id=0,
                        add_offset=0,
                        hash=0,
                    ),
                    operation_type="api",
                )
                return getattr(hist, "count", None)

            return await _safe_api_call(get_count)
        except Exception as e:
            logger.debug(
                f"Не удалось получить messages count для {group_identifier}: {e}"
            )
            return None

    async def get_messages(
        self,
        group_identifier: Union[str, int],
        limit: Optional[int] = None,
        min_id: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Получает сообщения группы с антиспам защитой

        Args:
            group_identifier: username группы (без @) или ID группы
            limit: Максимальное количество сообщений (None = все)
            min_id: Минимальный ID сообщения (для продолжения выгрузки)

        Returns:
            Список словарей с информацией о сообщениях
        """
        try:
            entity = await self._resolve_read_entity(group_identifier, allow_users=True)
            if not entity:
                logger.error(f"Не удалось найти группу: {group_identifier}")
                return []

            # Функция для безопасного получения сообщений
            async def fetch_messages_safe():
                messages = []
                count = 0

                async for msg in self.client.iter_messages(
                    entity,
                    limit=limit,
                    min_id=min_id,
                    reverse=False,  # От старых к новым
                ):
                    # Пропускаем служебные сообщения
                    if not msg.message and not msg.media:
                        continue

                    # Extract fwd_from info
                    fwd_from = None
                    if msg.fwd_from:
                        fwd = msg.fwd_from
                        fwd_from = {
                            "from_id": None,
                            "from_type": None,
                            "from_name": fwd.from_name,
                            "from_username": None,
                            "from_first_name": None,
                            "from_last_name": None,
                            "date": fwd.date.isoformat() if fwd.date else None,
                            "channel_post": fwd.channel_post,
                        }
                        if fwd.from_id:
                            from telethon.tl.types import (
                                PeerChannel,
                                PeerChat,
                                PeerUser,
                            )

                            if isinstance(fwd.from_id, PeerUser):
                                fwd_from["from_id"] = fwd.from_id.user_id
                                fwd_from["from_type"] = "user"
                            elif isinstance(fwd.from_id, PeerChannel):
                                fwd_from["from_id"] = fwd.from_id.channel_id
                                fwd_from["from_type"] = "channel"
                            elif isinstance(fwd.from_id, PeerChat):
                                fwd_from["from_id"] = fwd.from_id.chat_id
                                fwd_from["from_type"] = "chat"
                        # Resolve name/username from cached entities (no extra API calls)
                        # msg.forward.sender/.chat are populated from the iter_messages response
                        if msg.forward:
                            fwd_entity = msg.forward.sender or msg.forward.chat
                            if fwd_entity:
                                fwd_from["from_username"] = getattr(
                                    fwd_entity, "username", None
                                )
                                fwd_from["from_first_name"] = getattr(
                                    fwd_entity, "first_name", None
                                )
                                fwd_from["from_last_name"] = getattr(
                                    fwd_entity, "last_name", None
                                )

                    message_data = {
                        "id": msg.id,
                        "date": msg.date.isoformat() if msg.date else None,
                        "from_id": msg.from_id.user_id if msg.from_id else None,
                        "text": msg.message or "",
                        "fwd_from": fwd_from,
                        "is_reply": msg.reply_to is not None,
                        "reply_to_msg_id": (
                            msg.reply_to.reply_to_msg_id if msg.reply_to else None
                        ),
                        "views": getattr(msg, "views", None),
                        "forwards": getattr(msg, "forwards", None),
                        "is_pinned": getattr(msg, "is_pinned", False),
                        "has_media": msg.media is not None,
                        "media_type": type(msg.media).__name__ if msg.media else None,
                    }

                    messages.append(message_data)
                    count += 1

                    # Smart pause каждые 1000 сообщений
                    if count % 1000 == 0:
                        await smart_pause("participants", count)

                    # Проверка лимита
                    if limit and count >= limit:
                        break

                return messages

            # Вызываем через safe_call для анти-спам защиты
            messages = await _safe_api_call(fetch_messages_safe)

            logger.info(
                f"Получено {len(messages)} сообщений из группы {group_identifier}"
            )
            return messages

        except Exception as e:
            logger.error(
                f"Ошибка при получении сообщений группы {group_identifier}: {e}"
            )
            return []

    @staticmethod
    def _parse_cutoff_datetime(cutoff_iso: str) -> Optional[datetime]:
        raw = str(cutoff_iso or "").strip()
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            if len(raw) == 10:
                return datetime.fromisoformat(raw)
            return datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            return None

    @staticmethod
    def _media_metadata(msg) -> Dict[str, Any]:
        file_obj = getattr(msg, "file", None)
        return {
            "media_mime_type": getattr(file_obj, "mime_type", None),
            "media_filename": getattr(file_obj, "name", None),
            "media_size": getattr(file_obj, "size", None),
        }

    @classmethod
    def _message_to_record(cls, msg) -> Dict[str, Any]:
        fwd_from = None
        if getattr(msg, "fwd_from", None):
            fwd = msg.fwd_from
            fwd_from = {
                "from_id": None,
                "from_type": None,
                "from_name": fwd.from_name,
                "from_username": None,
                "from_first_name": None,
                "from_last_name": None,
                "date": fwd.date.isoformat() if fwd.date else None,
                "channel_post": fwd.channel_post,
            }
            if fwd.from_id:
                from telethon.tl.types import PeerChannel, PeerChat, PeerUser

                if isinstance(fwd.from_id, PeerUser):
                    fwd_from["from_id"] = fwd.from_id.user_id
                    fwd_from["from_type"] = "user"
                elif isinstance(fwd.from_id, PeerChannel):
                    fwd_from["from_id"] = fwd.from_id.channel_id
                    fwd_from["from_type"] = "channel"
                elif isinstance(fwd.from_id, PeerChat):
                    fwd_from["from_id"] = fwd.from_id.chat_id
            if getattr(msg, "forward", None):
                fwd_entity = msg.forward.sender or msg.forward.chat
                if fwd_entity:
                    fwd_from["from_username"] = getattr(fwd_entity, "username", None)
                    fwd_from["from_first_name"] = getattr(
                        fwd_entity, "first_name", None
                    )
                    fwd_from["from_last_name"] = getattr(fwd_entity, "last_name", None)

        record = {
            "id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "from_id": msg.from_id.user_id if msg.from_id else None,
            "text": msg.message or "",
            "caption": msg.message or "",
            "fwd_from": fwd_from,
            "is_reply": msg.reply_to is not None,
            "reply_to_msg_id": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
            "views": getattr(msg, "views", None),
            "forwards": getattr(msg, "forwards", None),
            "is_pinned": getattr(msg, "is_pinned", False),
            "has_media": msg.media is not None,
            "media_type": type(msg.media).__name__ if msg.media else None,
        }
        record.update(cls._media_metadata(msg))
        return record

    async def get_messages_since(
        self,
        group_identifier: Union[str, int],
        cutoff_iso: str,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """Read messages from newest to oldest until cutoff date is crossed."""
        try:
            entity = await self._resolve_read_entity(group_identifier, allow_users=True)
            if not entity:
                logger.error(f"Не удалось найти группу: {group_identifier}")
                return []

            cutoff_dt = self._parse_cutoff_datetime(cutoff_iso)
            if cutoff_dt is None:
                raise ValueError(f"Invalid cutoff_iso: {cutoff_iso!r}")
            normalized_limit = int(limit or 0)
            if normalized_limit < 0:
                raise ValueError("limit must be >= 0")

            async def fetch_messages_safe():
                messages = []
                count = 0
                iter_limit = normalized_limit if normalized_limit > 0 else None
                async for msg in self.client.iter_messages(
                    entity, limit=iter_limit, reverse=False
                ):
                    msg_dt = msg.date
                    if msg_dt is not None:
                        msg_naive = msg_dt.replace(tzinfo=None)
                        if msg_naive < cutoff_dt:
                            break

                    if not msg.message and not msg.media:
                        continue

                    messages.append(self._message_to_record(msg))
                    count += 1
                    if count % 1000 == 0:
                        await smart_pause("participants", count)
                    if normalized_limit and count >= normalized_limit:
                        break
                return messages

            messages = await _safe_api_call(fetch_messages_safe)
            logger.info(
                f"Получено {len(messages)} сообщений из {group_identifier} после {cutoff_iso}"
            )
            return messages
        except Exception as e:
            logger.error(
                f"Ошибка при получении сообщений since для {group_identifier}: {e}"
            )
            return []

    async def get_my_dialogs(
        self, limit: int = 100, dialog_type: str = "all"
    ) -> List[Dict[str, Any]]:
        """
        Получает список диалогов (групп/каналов/личных чатов) текущего аккаунта.

        Args:
            limit: максимальное количество диалогов
            dialog_type: фильтр по типу — "all", "group", "channel", "user"

        Returns:
            Список словарей с информацией о диалогах
        """
        try:

            async def fetch_dialogs():
                dialogs = []
                async for dialog in self.client.iter_dialogs(limit=limit):
                    dialogs.append(dialog)
                return dialogs

            dialogs = await _safe_api_call(fetch_dialogs)

            results = []
            for dialog in dialogs:
                if dialog.is_user:
                    dtype = "user"
                elif dialog.is_group:
                    dtype = "group"
                elif dialog.is_channel:
                    dtype = "channel"
                else:
                    dtype = "other"

                if dialog_type != "all" and dtype != dialog_type:
                    continue

                results.append(
                    {
                        "id": dialog.id,
                        "title": dialog.title or dialog.name or "Untitled",
                        "type": dtype,
                        "username": getattr(dialog.entity, "username", None),
                        "phone": (
                            getattr(dialog.entity, "phone", None)
                            if dtype == "user"
                            else None
                        ),
                        "participants_count": getattr(
                            dialog.entity, "participants_count", None
                        ),
                        "unread_count": dialog.unread_count,
                    }
                )

            logger.info(f"Получено {len(results)} диалогов (фильтр: {dialog_type})")
            return results

        except FloodWaitError as e:
            logger.error(
                f"Превышен лимит запросов при получении диалогов. Ожидание {e.seconds} секунд"
            )
            return []
        except Exception as e:
            logger.error(f"Ошибка при получении диалогов: {e}")
            return []

    async def resolve_username(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Резолвит username в информацию о пользователе/канале/чате.

        Args:
            username: Telegram username (с или без @)

        Returns:
            Словарь с id, type, username, first_name, last_name и т.д., или None
        """
        try:
            if not username.startswith("@"):
                username = "@" + username

            entity = await _safe_api_call(self.client.get_entity, username)

            if isinstance(entity, User):
                return {
                    "id": entity.id,
                    "type": "user",
                    "username": entity.username,
                    "first_name": entity.first_name,
                    "last_name": entity.last_name,
                    "is_bot": entity.bot,
                    "is_premium": getattr(entity, "premium", False),
                }
            elif isinstance(entity, Channel):
                return {
                    "id": entity.id,
                    "type": "channel" if entity.broadcast else "supergroup",
                    "username": entity.username,
                    "title": entity.title,
                    "participants_count": getattr(entity, "participants_count", None),
                }
            elif isinstance(entity, Chat):
                return {
                    "id": entity.id,
                    "type": "chat",
                    "title": entity.title,
                    "participants_count": getattr(entity, "participants_count", None),
                }
            else:
                return {
                    "id": getattr(entity, "id", None),
                    "type": type(entity).__name__,
                }

        except Exception as e:
            logger.error(f"Ошибка при resolve username {username}: {e}")
            return None

    async def download_media(
        self, group_identifier: Union[str, int], message_id: int, output_dir: str
    ) -> Optional[str]:
        """
        Скачивает медиа/файл из сообщения в указанную директорию.

        Args:
            group_identifier: username группы (без @) или ID группы
            message_id: ID сообщения с медиа
            output_dir: директория для сохранения файла

        Returns:
            Путь к скачанному файлу или None при ошибке
        """
        try:
            entity = await self._resolve_read_entity(group_identifier, allow_users=True)
            if not entity:
                logger.error(f"Не удалось найти группу: {group_identifier}")
                return None

            async def do_download():
                msgs = await self.client.get_messages(entity, ids=message_id)
                if not msgs or not msgs.media:
                    logger.error(f"Message {message_id} has no media")
                    return None
                os.makedirs(output_dir, exist_ok=True)
                return await self.client.download_media(msgs.media, output_dir)

            path = await _safe_api_call(do_download)
            if path:
                logger.info(f"Downloaded media from message {message_id} to {path}")
            return path

        except Exception as e:
            logger.error(f"Error downloading media from message {message_id}: {e}")
            return None

    @staticmethod
    def _normalize_user_identifier(user_identifier: Union[str, int]) -> Union[str, int]:
        """Нормализует идентификатор пользователя (ID или @username)."""
        if isinstance(user_identifier, int):
            return user_identifier

        raw = str(user_identifier).strip()
        if not raw:
            raise ValueError("User identifier is empty")

        stripped = raw.lstrip("-")
        if stripped.isdigit():
            return int(raw)

        return raw if raw.startswith("@") else "@" + raw

    @staticmethod
    def _normalize_message_ids(
        message_ids: Union[int, List[int], tuple, set],
    ) -> List[int]:
        """Нормализует и дедуплицирует IDs сообщений с сохранением порядка."""
        raw_items = (
            [message_ids] if isinstance(message_ids, int) else list(message_ids or [])
        )
        normalized: List[int] = []
        seen = set()
        for item in raw_items:
            try:
                message_id = int(item)
            except Exception as exc:
                raise ValueError(f"Invalid message id: {item!r}") from exc
            if message_id <= 0:
                raise ValueError(f"Invalid message id: {message_id}")
            if message_id in seen:
                continue
            seen.add(message_id)
            normalized.append(message_id)
        if not normalized:
            raise ValueError("message_ids is empty")
        return normalized

    @staticmethod
    def _entity_target(entity: Union[Channel, Chat]) -> str:
        username = getattr(entity, "username", None)
        if username:
            return f"@{username}"
        return str(getattr(entity, "id", ""))

    async def _resolve_linked_discussion_group(
        self,
        channel_identifier: Union[str, int],
    ) -> tuple[Channel, dict[str, Any]]:
        channel_entity = await self._resolve_group_entity_for_admin(channel_identifier)
        if not isinstance(channel_entity, Channel):
            raise ValueError(
                f"Target '{channel_identifier}' is not a channel/supergroup"
            )

        from telethon.tl.functions.channels import GetFullChannelRequest

        full_info = await _safe_api_call(
            self.client,
            GetFullChannelRequest(channel_entity),
            operation_type="api",
        )
        full_chat = getattr(full_info, "full_chat", None)
        linked_chat_id = getattr(full_chat, "linked_chat_id", None)
        if not linked_chat_id:
            raise ValueError(
                f"Channel '{channel_identifier}' has no linked discussion group"
            )

        linked_entity = None
        for item in getattr(full_info, "chats", []) or []:
            if getattr(item, "id", None) == linked_chat_id:
                linked_entity = item
                break

        if linked_entity is None:
            linked_entity = await _safe_api_call(self.client.get_entity, linked_chat_id)

        if not isinstance(linked_entity, Channel):
            raise ValueError(
                f"Linked discussion target for '{channel_identifier}' is not a supergroup"
            )

        info = {
            "channel_id": channel_entity.id,
            "channel_title": getattr(channel_entity, "title", None),
            "channel_username": getattr(channel_entity, "username", None),
            "channel_target": self._entity_target(channel_entity),
            "linked_group_id": linked_entity.id,
            "linked_group_title": getattr(linked_entity, "title", None),
            "linked_group_username": getattr(linked_entity, "username", None),
            "linked_group_target": self._entity_target(linked_entity),
            "current_join_required": getattr(linked_entity, "join_to_send", None),
        }
        return linked_entity, info

    async def _resolve_group_entity_for_admin(
        self, group_identifier: Union[str, int]
    ) -> Union[Channel, Chat]:
        """Резолвит группу/канал для admin-операций."""
        if isinstance(group_identifier, int):
            entity_id: Union[str, int] = group_identifier
        elif (
            isinstance(group_identifier, str)
            and group_identifier.startswith("-")
            and group_identifier[1:].isdigit()
        ):
            entity_id = int(group_identifier)
        else:
            is_valid, error_msg = _validate_group_identifier(str(group_identifier))
            if not is_valid:
                raise ValueError(error_msg)
            entity_id = (
                str(group_identifier)
                if str(group_identifier).startswith("@")
                else "@" + str(group_identifier)
            )

        entity = await _safe_api_call(self.client.get_entity, entity_id)
        if not isinstance(entity, (Channel, Chat)):
            raise ValueError(f"Target '{group_identifier}' is not a group/channel")
        return entity

    async def _resolve_user_entity_for_admin(
        self, user_identifier: Union[str, int]
    ) -> User:
        """Резолвит целевого пользователя для admin-операций."""
        normalized = self._normalize_user_identifier(user_identifier)
        entity = await _safe_api_call(self.client.get_entity, normalized)
        if not isinstance(entity, User):
            raise ValueError(f"Target '{user_identifier}' is not a Telegram user")
        return entity

    async def create_private_group(
        self,
        title: str,
        users: Optional[List[Union[str, int]]] = None,
        about: str = "",
        kind: str = "basic",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Create a private Telegram group and optionally invite users."""
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("Group title is empty")

        group_kind = str(kind or "basic").strip().lower()
        if group_kind not in {"basic", "supergroup"}:
            raise ValueError("kind must be 'basic' or 'supergroup'")

        invitees = list(users or [])
        if group_kind == "basic" and not invitees:
            raise ValueError("basic group creation requires at least one invitee")

        resolved_users = []
        user_entities = []
        for user_identifier in invitees:
            user_entity = await self._resolve_user_entity_for_admin(user_identifier)
            user_entities.append(user_entity)
            resolved_users.append(
                {
                    "input": str(user_identifier),
                    "user_id": user_entity.id,
                    "username": user_entity.username,
                    "first_name": user_entity.first_name,
                    "is_bot": user_entity.bot,
                }
            )

        result: Dict[str, Any] = {
            "success": True,
            "action": "create_private_group",
            "dry_run": dry_run,
            "title": clean_title,
            "about": str(about or ""),
            "kind": group_kind,
            "invitees": resolved_users,
        }
        if dry_run:
            return result

        invite_results = []
        if group_kind == "basic":
            from telethon.tl.functions.messages import CreateChatRequest

            created = await _safe_api_call(
                self.client,
                CreateChatRequest(users=user_entities, title=clean_title),
                operation_type="join",
            )
            for user_identifier, user_entity in zip(invitees, user_entities):
                invite_results.append(
                    {
                        "success": True,
                        "created_with_group": True,
                        "user": str(user_identifier),
                        "user_id": user_entity.id,
                        "username": user_entity.username,
                    }
                )
        else:
            from telethon.tl.functions.channels import CreateChannelRequest

            created = await _safe_api_call(
                self.client,
                CreateChannelRequest(
                    title=clean_title,
                    about=str(about or ""),
                    megagroup=True,
                    broadcast=False,
                ),
                operation_type="join",
            )

        group_entity = None
        for chat in getattr(created, "chats", []) or []:
            if (
                isinstance(chat, (Channel, Chat))
                and getattr(chat, "title", None) == clean_title
            ):
                group_entity = chat
                break
        if group_entity is None:
            for chat in getattr(created, "chats", []) or []:
                if isinstance(chat, (Channel, Chat)):
                    group_entity = chat
                    break
        if group_entity is None:
            group_entity = await self._resolve_dialog_entity_by_title(
                clean_title, allow_users=False
            )
        if group_entity is None:
            raise RuntimeError("Telegram did not return created group entity")

        if group_kind == "supergroup":
            from telethon.tl.functions.channels import InviteToChannelRequest

            for user_identifier, user_entity in zip(invitees, user_entities):
                try:
                    if isinstance(group_entity, Channel):
                        await _safe_api_call(
                            self.client,
                            InviteToChannelRequest(
                                channel=group_entity, users=[user_entity]
                            ),
                            operation_type="join",
                        )
                    else:
                        from telethon.tl.functions.messages import AddChatUserRequest

                        await _safe_api_call(
                            self.client,
                            AddChatUserRequest(
                                chat_id=group_entity.id,
                                user_id=user_entity,
                                fwd_limit=0,
                            ),
                            operation_type="join",
                        )
                    invite_results.append(
                        {
                            "success": True,
                            "user": str(user_identifier),
                            "user_id": user_entity.id,
                            "username": user_entity.username,
                        }
                    )
                except UserAlreadyParticipantError:
                    invite_results.append(
                        {
                            "success": True,
                            "already_member": True,
                            "user": str(user_identifier),
                            "user_id": user_entity.id,
                            "username": user_entity.username,
                        }
                    )
                except Exception as exc:
                    invite_results.append(
                        {
                            "success": False,
                            "user": str(user_identifier),
                            "user_id": user_entity.id,
                            "username": user_entity.username,
                            "error": str(exc),
                        }
                    )

        result.update(
            {
                "group_id": group_entity.id,
                "group_title": getattr(group_entity, "title", clean_title),
                "group_username": getattr(group_entity, "username", None),
                "group_type": (
                    "channel" if isinstance(group_entity, Channel) else "group"
                ),
                "invites": invite_results,
            }
        )
        return result

    async def set_channel_comments_join_requirement(
        self,
        channel_identifier: Union[str, int],
        join_required: bool,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Включает/выключает требование join-to-send у linked discussion group канала."""
        try:
            linked_entity, info = await self._resolve_linked_discussion_group(
                channel_identifier
            )
            result = {
                "success": True,
                "action": "set_channel_comments_join_requirement",
                "dry_run": dry_run,
                "join_required": bool(join_required),
                **info,
            }

            if dry_run:
                return result

            from telethon.tl.functions.channels import ToggleJoinToSendRequest

            await _safe_api_call(
                self.client,
                ToggleJoinToSendRequest(
                    channel=linked_entity,
                    enabled=bool(join_required),
                ),
                operation_type="api",
            )
            result["current_join_required"] = bool(join_required)
            return result
        except Exception as e:
            logger.error(
                "Error updating comments join requirement for %s: %s",
                channel_identifier,
                e,
            )
            return {
                "success": False,
                "action": "set_channel_comments_join_requirement",
                "dry_run": dry_run,
                "channel": str(channel_identifier),
                "join_required": bool(join_required),
                "error": str(e),
            }

    async def add_member_to_group(
        self,
        group_identifier: Union[str, int],
        user_identifier: Union[str, int],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Добавляет пользователя в группу/канал с anti-spam защитой."""
        try:
            group_entity = await self._resolve_group_entity_for_admin(group_identifier)
            user_entity = await self._resolve_user_entity_for_admin(user_identifier)

            group_type = "channel" if isinstance(group_entity, Channel) else "group"
            result = {
                "success": True,
                "action": "add_member",
                "dry_run": dry_run,
                "group_id": group_entity.id,
                "group_type": group_type,
                "user_id": user_entity.id,
                "user_username": user_entity.username,
            }

            if dry_run:
                return result

            if isinstance(group_entity, Channel):
                from telethon.tl.functions.channels import InviteToChannelRequest

                await _safe_api_call(
                    self.client,
                    InviteToChannelRequest(channel=group_entity, users=[user_entity]),
                    operation_type="join",
                )
            else:
                from telethon.tl.functions.messages import AddChatUserRequest

                await _safe_api_call(
                    self.client,
                    AddChatUserRequest(
                        chat_id=group_entity.id,
                        user_id=user_entity,
                        fwd_limit=0,
                    ),
                    operation_type="join",
                )

            return result
        except UserAlreadyParticipantError:
            return {
                "success": True,
                "action": "add_member",
                "already_member": True,
                "dry_run": dry_run,
                "group": str(group_identifier),
                "user": str(user_identifier),
            }
        except Exception as e:
            logger.error(
                f"Error adding user {user_identifier} to group {group_identifier}: {e}"
            )
            return {
                "success": False,
                "action": "add_member",
                "dry_run": dry_run,
                "group": str(group_identifier),
                "user": str(user_identifier),
                "error": str(e),
            }

    async def remove_member_from_group(
        self,
        group_identifier: Union[str, int],
        user_identifier: Union[str, int],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Удаляет пользователя из группы/канала (kick+unban для каналов)."""
        try:
            group_entity = await self._resolve_group_entity_for_admin(group_identifier)
            user_entity = await self._resolve_user_entity_for_admin(user_identifier)

            group_type = "channel" if isinstance(group_entity, Channel) else "group"
            result = {
                "success": True,
                "action": "remove_member",
                "dry_run": dry_run,
                "group_id": group_entity.id,
                "group_type": group_type,
                "user_id": user_entity.id,
                "user_username": user_entity.username,
            }

            if dry_run:
                return result

            if isinstance(group_entity, Channel):
                from telethon.tl.functions.channels import EditBannedRequest
                from telethon.tl.types import ChatBannedRights

                ban_rights = ChatBannedRights(until_date=None, view_messages=True)
                unban_rights = ChatBannedRights(until_date=None, view_messages=False)

                await _safe_api_call(
                    self.client,
                    EditBannedRequest(
                        channel=group_entity,
                        participant=user_entity,
                        banned_rights=ban_rights,
                    ),
                    operation_type="join",
                )
                await _safe_api_call(
                    self.client,
                    EditBannedRequest(
                        channel=group_entity,
                        participant=user_entity,
                        banned_rights=unban_rights,
                    ),
                    operation_type="join",
                )
            else:
                from telethon.tl.functions.messages import DeleteChatUserRequest

                await _safe_api_call(
                    self.client,
                    DeleteChatUserRequest(
                        chat_id=group_entity.id,
                        user_id=user_entity,
                        revoke_history=False,
                    ),
                    operation_type="join",
                )

            return result
        except UserNotParticipantError:
            return {
                "success": True,
                "action": "remove_member",
                "not_participant": True,
                "dry_run": dry_run,
                "group": str(group_identifier),
                "user": str(user_identifier),
            }
        except Exception as e:
            logger.error(
                f"Error removing user {user_identifier} from group {group_identifier}: {e}"
            )
            return {
                "success": False,
                "action": "remove_member",
                "dry_run": dry_run,
                "group": str(group_identifier),
                "user": str(user_identifier),
                "error": str(e),
            }

    async def migrate_member(
        self,
        group_identifier: Union[str, int],
        old_user_identifier: Union[str, int],
        new_user_identifier: Union[str, int],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Миграция участника: добавить новый аккаунт, затем удалить старый."""
        if str(old_user_identifier).strip() == str(new_user_identifier).strip():
            return {
                "success": False,
                "action": "migrate_member",
                "dry_run": dry_run,
                "group": str(group_identifier),
                "error": "old_user_identifier and new_user_identifier are the same",
            }

        add_preview = await self.add_member_to_group(
            group_identifier,
            new_user_identifier,
            dry_run=True,
        )
        remove_preview = await self.remove_member_from_group(
            group_identifier,
            old_user_identifier,
            dry_run=True,
        )

        if dry_run:
            return {
                "success": add_preview.get("success", False)
                and remove_preview.get("success", False),
                "action": "migrate_member",
                "dry_run": True,
                "group": str(group_identifier),
                "add_new_user": add_preview,
                "remove_old_user": remove_preview,
            }

        add_result = await self.add_member_to_group(
            group_identifier,
            new_user_identifier,
            dry_run=False,
        )
        if not add_result.get("success"):
            return {
                "success": False,
                "action": "migrate_member",
                "dry_run": False,
                "group": str(group_identifier),
                "error": "Failed to add new user; old user was not removed",
                "add_new_user": add_result,
                "remove_old_user": None,
            }

        remove_result = await self.remove_member_from_group(
            group_identifier,
            old_user_identifier,
            dry_run=False,
        )
        return {
            "success": bool(remove_result.get("success")),
            "action": "migrate_member",
            "dry_run": False,
            "group": str(group_identifier),
            "add_new_user": add_result,
            "remove_old_user": remove_result,
        }

    async def send_message(
        self, group_identifier: Union[str, int], message_text: str
    ) -> bool:
        """
        Отправляет сообщение в группу с антиспам защитой

        Args:
            group_identifier: ID группы, username или название
            message_text: Текст сообщения для отправки

        Returns:
            True если сообщение отправлено успешно, False в случае ошибки
        """
        try:
            entity = await self._resolve_target_entity(group_identifier)

            # Отправляем сообщение через safe_call с антиспам защитой
            await _safe_api_call(
                self.client.send_message,
                entity,
                message_text,
                operation_type="group_msg",
            )

            logger.info(f"Сообщение успешно отправлено в группу {group_identifier}")
            return True

        except Exception as e:
            logger.error(
                f"Ошибка при отправке сообщения в группу {group_identifier}: {e}"
            )
            return False

    async def send_file(
        self, group_identifier: Union[str, int], file_path: str, caption: str = ""
    ) -> bool:
        """Отправляет файл в группу/чат с антиспам защитой."""
        try:
            entity = await self._resolve_target_entity(group_identifier)
            await _safe_api_call(
                self.client.send_file,
                entity,
                file_path,
                caption=caption,
                operation_type="group_msg",
            )
            logger.info(f"Файл успешно отправлен в группу {group_identifier}")
            return True
        except Exception as e:
            logger.error(f"Ошибка при отправке файла в группу {group_identifier}: {e}")
            return False

    async def delete_messages(
        self,
        group_identifier: Union[str, int],
        message_ids: Union[int, List[int], tuple, set],
        revoke: bool = True,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Удаляет конкретные сообщения в группе/чате."""
        try:
            entity = await self._resolve_target_entity(group_identifier)
            normalized_ids = self._normalize_message_ids(message_ids)
            result = {
                "success": True,
                "action": "delete_messages",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "message_ids": normalized_ids,
                "message_count": len(normalized_ids),
                "revoke": bool(revoke),
            }

            if dry_run:
                return result

            await _safe_api_call(
                self.client.delete_messages,
                entity,
                normalized_ids,
                revoke=bool(revoke),
                operation_type="api",
            )
            return result
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщений в {group_identifier}: {e}")
            return {
                "success": False,
                "action": "delete_messages",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "error": str(e),
            }

    async def clear_history(
        self,
        group_identifier: Union[str, int],
        max_id: int = 0,
        revoke: bool = True,
        just_clear: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Очищает историю диалога/чата через DeleteHistoryRequest."""
        try:
            entity = await self._resolve_target_entity(group_identifier)
            normalized_max_id = int(max_id or 0)
            if normalized_max_id < 0:
                raise ValueError("max_id must be >= 0")

            result = {
                "success": True,
                "action": "clear_history",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "max_id": normalized_max_id,
                "revoke": bool(revoke),
                "just_clear": bool(just_clear),
            }

            if dry_run:
                return result

            from telethon.tl.functions.messages import DeleteHistoryRequest

            await _safe_api_call(
                self.client,
                DeleteHistoryRequest(
                    peer=entity,
                    max_id=normalized_max_id,
                    revoke=bool(revoke),
                    just_clear=bool(just_clear),
                    min_date=None,
                    max_date=None,
                ),
                operation_type="api",
            )
            return result
        except Exception as e:
            logger.error(f"Ошибка при очистке истории {group_identifier}: {e}")
            return {
                "success": False,
                "action": "clear_history",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "error": str(e),
            }

    async def leave_dialog(
        self,
        group_identifier: Union[str, int],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Leave a channel/supergroup/basic group. Direct user dialogs are not leaveable."""
        try:
            entity = await self._resolve_target_entity(group_identifier)
            if isinstance(entity, User):
                return {
                    "success": False,
                    "action": "leave_dialog",
                    "dry_run": dry_run,
                    "target": str(group_identifier),
                    "error": (
                        "Direct user dialogs cannot be left; delete messages "
                        "or clear history instead."
                    ),
                }

            if isinstance(entity, Channel):
                dialog_type = "channel" if entity.broadcast else "supergroup"
            elif isinstance(entity, Chat):
                dialog_type = "group"
            else:
                dialog_type = type(entity).__name__

            result = {
                "success": True,
                "action": "leave_dialog",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "dialog_id": getattr(entity, "id", None),
                "dialog_type": dialog_type,
                "title": getattr(entity, "title", None),
                "username": getattr(entity, "username", None),
            }

            if dry_run:
                return result

            if isinstance(entity, Channel):
                from telethon.tl.functions.channels import LeaveChannelRequest

                await _safe_api_call(
                    self.client,
                    LeaveChannelRequest(channel=entity),
                    operation_type="join",
                )
                return result

            if isinstance(entity, Chat):
                from telethon.tl.functions.messages import DeleteChatUserRequest

                me = await _safe_api_call(self.client.get_me, operation_type="api")
                await _safe_api_call(
                    self.client,
                    DeleteChatUserRequest(
                        chat_id=entity.id,
                        user_id=me,
                        revoke_history=False,
                    ),
                    operation_type="join",
                )
                return result

            return {
                **result,
                "success": False,
                "error": f"Unsupported dialog entity type: {type(entity).__name__}",
            }
        except Exception as e:
            logger.error(f"Ошибка при выходе из диалога {group_identifier}: {e}")
            return {
                "success": False,
                "action": "leave_dialog",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "error": str(e),
            }

    async def edit_message(
        self,
        group_identifier: Union[str, int],
        message_id: int,
        new_text: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Редактирует сообщение в диалоге/чате."""
        try:
            entity = await self._resolve_target_entity(group_identifier)
            normalized_message_id = int(message_id)
            if normalized_message_id <= 0:
                raise ValueError("message_id must be > 0")

            clean_text = str(new_text or "").strip()
            if not clean_text:
                raise ValueError("new_text is empty")

            result = {
                "success": True,
                "action": "edit_message",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "message_id": normalized_message_id,
                "new_text_len": len(clean_text),
            }

            if dry_run:
                return result

            await _safe_api_call(
                self.client.edit_message,
                entity,
                normalized_message_id,
                clean_text,
                operation_type="group_msg",
            )
            return result
        except Exception as e:
            logger.error(
                f"Ошибка при редактировании сообщения {message_id} в {group_identifier}: {e}"
            )
            return {
                "success": False,
                "action": "edit_message",
                "dry_run": dry_run,
                "target": str(group_identifier),
                "error": str(e),
            }

    async def forward_messages(
        self,
        from_group_identifier: Union[str, int],
        to_group_identifier: Union[str, int],
        message_ids: Union[int, List[int], tuple, set],
        drop_author: bool = False,
        drop_media_captions: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Пересылает сообщения из одного диалога в другой."""
        try:
            source_entity = await self._resolve_target_entity(from_group_identifier)
            target_entity = await self._resolve_target_entity(to_group_identifier)
            normalized_ids = self._normalize_message_ids(message_ids)
            result = {
                "success": True,
                "action": "forward_messages",
                "dry_run": dry_run,
                "from_target": str(from_group_identifier),
                "to_target": str(to_group_identifier),
                "message_ids": normalized_ids,
                "message_count": len(normalized_ids),
                "drop_author": bool(drop_author),
                "drop_media_captions": bool(drop_media_captions),
            }

            if dry_run:
                return result

            await _safe_api_call(
                self.client.forward_messages,
                target_entity,
                normalized_ids,
                from_peer=source_entity,
                drop_author=bool(drop_author),
                drop_media_captions=bool(drop_media_captions),
                operation_type="group_msg",
            )
            return result
        except Exception as e:
            logger.error(
                "Ошибка при пересылке сообщений %s -> %s: %s",
                from_group_identifier,
                to_group_identifier,
                e,
            )
            return {
                "success": False,
                "action": "forward_messages",
                "dry_run": dry_run,
                "from_target": str(from_group_identifier),
                "to_target": str(to_group_identifier),
                "error": str(e),
            }

    async def _resolve_target_entity(self, group_identifier: Union[str, int]):
        """Разрешает target в Telethon entity для write-операций."""
        entity = None

        if isinstance(group_identifier, int):
            return await _safe_api_call(self.client.get_entity, group_identifier)

        if isinstance(group_identifier, str) and group_identifier.isdigit():
            return await _safe_api_call(self.client.get_entity, int(group_identifier))

        if isinstance(group_identifier, str) and (
            group_identifier.startswith("-") and group_identifier[1:].isdigit()
        ):
            return await _safe_api_call(self.client.get_entity, int(group_identifier))

        if isinstance(group_identifier, str) and " " in group_identifier:
            # Название с пробелами — ищем только через диалоги
            logger.info(f"Searching for group by title: '{group_identifier}'")

            async def search_dialogs():
                dialogs = []
                async for dialog in self.client.iter_dialogs(limit=500):
                    if (
                        dialog.title
                        and group_identifier.lower() in dialog.title.lower()
                    ):
                        dialogs.append(dialog)
                return dialogs

            dialogs = await _safe_api_call(search_dialogs)
            if not dialogs:
                raise ValueError(f"Группа '{group_identifier}' не найдена в диалогах")

            for dialog in dialogs:
                if dialog.title.lower() == group_identifier.lower():
                    entity = dialog.entity
                    break
            if entity is None:
                entity = dialogs[0].entity
            return entity

        is_valid, error_msg = _validate_group_identifier(str(group_identifier))
        if not is_valid:
            raise ValueError(error_msg)

        # Username target: allow both groups/channels and direct user dialogs.
        # This keeps write actions usable for 1:1 assignment delivery via ActionMCP.
        username_target = (
            str(group_identifier)
            if str(group_identifier).startswith("@")
            else "@" + str(group_identifier)
        )
        try:
            entity = await _safe_api_call(self.client.get_entity, username_target)
            if isinstance(entity, (Channel, Chat, User)):
                return entity
        except Exception:
            pass

        group_info = await self.get_group_info(str(group_identifier))
        if group_info:
            return await _safe_api_call(self.client.get_entity, group_info["id"])
        raise ValueError(f"Группа '{group_identifier}' не найдена")
