"""
Тесты для GroupManager
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.errors.rpcerrorlist import (
    UserAlreadyParticipantError,
    UserNotParticipantError,
)
from telethon.tl.types import Channel, Chat, User

import tganalytics.domain.groups as groups_module
from tganalytics.domain.groups import GroupManager


@pytest.mark.asyncio
async def test_get_group_info_success(mock_telegram_client, mock_channel):
    """Тест успешного получения информации о группе"""
    # Настройка мока
    mock_telegram_client.get_entity.return_value = mock_channel

    # Создание менеджера
    group_manager = GroupManager(mock_telegram_client)

    # Выполнение теста
    result = await group_manager.get_group_info("testgroup")

    # Проверки
    assert result is not None
    assert result["id"] == mock_channel.id
    assert result["title"] == mock_channel.title
    assert result["username"] == mock_channel.username
    assert result["participants_count"] == mock_channel.participants_count
    assert result["type"] == "channel"

    # Проверка вызова
    mock_telegram_client.get_entity.assert_called_once_with("@testgroup")


@pytest.mark.asyncio
async def test_get_group_info_without_at_prefix(mock_telegram_client, mock_channel):
    """Тест получения информации о группе без @ префикса"""
    mock_telegram_client.get_entity.return_value = mock_channel

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_group_info("testgroup")

    assert result is not None
    mock_telegram_client.get_entity.assert_called_once_with("@testgroup")


@pytest.mark.asyncio
async def test_get_group_info_not_found(mock_telegram_client):
    """Тест обработки случая, когда группа не найдена"""
    mock_telegram_client.get_entity.side_effect = Exception("Group not found")

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_group_info("nonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_get_group_info_resolves_dialog_title_with_spaces(
    mock_telegram_client, mock_channel
):
    """Тест fallback на title диалога, если передали имя группы с пробелами."""
    from tests.conftest import AsyncIteratorMock

    mock_channel.title = "attia project"
    mock_telegram_client.iter_dialogs.return_value = AsyncIteratorMock(
        [SimpleNamespace(entity=mock_channel)]
    )

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_group_info("attia project")

    assert result is not None
    assert result["id"] == mock_channel.id
    assert result["title"] == "attia project"


@pytest.mark.asyncio
async def test_get_group_info_supports_direct_user_dialog(
    mock_telegram_client, mock_user
):
    """Direct dialog info должен возвращаться и для user target."""
    mock_telegram_client.get_entity.return_value = mock_user

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_group_info("test_user")

    assert result is not None
    assert result["id"] == mock_user.id
    assert result["type"] == "user"
    assert result["title"] == "Test User"
    assert result["username"] == "test_user"


@pytest.mark.asyncio
async def test_get_group_info_resolves_direct_user_dialog_title(
    mock_telegram_client, mock_user
):
    """Title-based lookup должен находить и 1:1 dialogs."""
    from tests.conftest import AsyncIteratorMock

    mock_telegram_client.iter_dialogs.return_value = AsyncIteratorMock(
        [SimpleNamespace(entity=mock_user, title="Test User", name="Test User")]
    )

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_group_info("Test User")

    assert result is not None
    assert result["id"] == mock_user.id
    assert result["type"] == "user"
    assert result["title"] == "Test User"


@pytest.mark.asyncio
async def test_get_participants_success(
    mock_telegram_client, mock_channel, mock_participants_iterator
):
    """Тест успешного получения участников группы"""
    # Настройка моков
    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.iter_participants.return_value = mock_participants_iterator

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.get_participants("testgroup", limit=10)

    assert len(participants) == 1
    assert participants[0]["id"] == 123456789
    assert participants[0]["username"] == "test_user"
    assert participants[0]["first_name"] == "Test"
    assert participants[0]["is_bot"] is False


@pytest.mark.asyncio
async def test_get_participants_resolves_dialog_title_with_spaces(
    mock_telegram_client, mock_channel, mock_participants_iterator
):
    """Title с пробелами должен работать и для participants."""
    from tests.conftest import AsyncIteratorMock

    mock_channel.title = "attia project"
    mock_telegram_client.iter_dialogs.return_value = AsyncIteratorMock(
        [SimpleNamespace(entity=mock_channel)]
    )
    mock_telegram_client.iter_participants.return_value = mock_participants_iterator

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.get_participants("attia project", limit=10)

    assert len(participants) == 1
    mock_telegram_client.iter_participants.assert_called_once_with(
        mock_channel, limit=10
    )


@pytest.mark.asyncio
async def test_get_participants_exclude_bots(
    mock_telegram_client, mock_channel, mock_bot_and_user_iterator
):
    """Тест исключения ботов из списка участников"""
    # Настройка моков
    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.iter_participants.return_value = mock_bot_and_user_iterator

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.get_participants("testgroup", limit=10)

    # Должен быть только обычный пользователь, бот исключен
    assert len(participants) == 1
    assert participants[0]["username"] == "regular_user"
    assert participants[0]["is_bot"] is False


@pytest.mark.asyncio
async def test_get_participants_admin_required_error(
    mock_telegram_client, mock_channel
):
    """Тест обработки ошибки отсутствия прав администратора"""
    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.iter_participants.side_effect = ChatAdminRequiredError(
        "No admin rights"
    )

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.get_participants("testgroup")

    assert participants == []


@pytest.mark.asyncio
async def test_get_participants_flood_wait_error(mock_telegram_client, mock_channel):
    """Тест обработки ошибки превышения лимита запросов"""
    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.iter_participants.side_effect = FloodWaitError(60)

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.get_participants("testgroup")

    assert participants == []


@pytest.mark.asyncio
async def test_search_participants_success(
    mock_telegram_client, mock_channel, mock_participants_iterator
):
    """Тест успешного поиска участников"""
    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.iter_participants.return_value = mock_participants_iterator

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.search_participants(
        "testgroup", "test", limit=10
    )

    assert len(participants) == 1
    assert participants[0]["username"] == "test_user"

    # Проверяем, что поиск вызван с entity, а не сырым username.
    mock_telegram_client.iter_participants.assert_called_once_with(
        mock_channel, search="test", limit=10
    )


@pytest.mark.asyncio
async def test_search_participants_empty_result(mock_telegram_client):
    """Тест поиска участников с пустым результатом"""
    from tests.conftest import AsyncIteratorMock

    mock_telegram_client.iter_participants.return_value = AsyncIteratorMock([])

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.search_participants("testgroup", "nonexistent")

    assert participants == []


@pytest.mark.asyncio
async def test_search_participants_resolves_dialog_title_with_spaces(
    mock_telegram_client, mock_channel, mock_participants_iterator
):
    """Title с пробелами должен работать и для search."""
    from tests.conftest import AsyncIteratorMock

    mock_channel.title = "attia project"
    mock_telegram_client.iter_dialogs.return_value = AsyncIteratorMock(
        [SimpleNamespace(entity=mock_channel)]
    )
    mock_telegram_client.iter_participants.return_value = mock_participants_iterator

    group_manager = GroupManager(mock_telegram_client)
    participants = await group_manager.search_participants(
        "attia project", "test", limit=10
    )

    assert len(participants) == 1
    mock_telegram_client.iter_participants.assert_called_once_with(
        mock_channel, search="test", limit=10
    )


@pytest.mark.asyncio
async def test_export_participants_to_csv_success(
    mock_telegram_client, mock_channel, sample_participants, tmp_path
):
    """Тест успешного экспорта участников в CSV"""
    # Создаем моки пользователей из sample_participants
    from tests.conftest import AsyncIteratorMock

    mock_users = []
    for participant in sample_participants:
        user = MagicMock(spec=User)
        user.id = participant["id"]
        user.username = participant["username"]
        user.first_name = participant["first_name"]
        user.last_name = participant["last_name"]
        user.phone = participant["phone"]
        user.bot = participant["is_bot"]
        user.verified = participant["is_verified"]
        user.premium = participant["is_premium"]
        user.status = participant["status"]
        mock_users.append(user)

    # Настройка моков
    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.iter_participants.return_value = AsyncIteratorMock(mock_users)

    group_manager = GroupManager(mock_telegram_client)

    # Создаем временный файл
    csv_file = tmp_path / "test_participants.csv"

    # Выполняем экспорт
    result = await group_manager.export_participants_to_csv(
        "testgroup", str(csv_file), limit=10
    )

    assert result is True
    assert csv_file.exists()

    # Проверяем содержимое файла
    content = csv_file.read_text(encoding="utf-8")
    assert (
        "id,username,first_name,last_name,phone,is_verified,is_premium,status"
        in content
    )
    assert "user1" in content
    assert "user2" in content


@pytest.mark.asyncio
async def test_export_participants_to_csv_no_participants(
    mock_telegram_client, mock_channel, tmp_path
):
    """Тест экспорта при отсутствии участников"""
    from tests.conftest import AsyncIteratorMock

    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.iter_participants.return_value = AsyncIteratorMock([])

    group_manager = GroupManager(mock_telegram_client)
    csv_file = tmp_path / "empty_participants.csv"

    result = await group_manager.export_participants_to_csv("testgroup", str(csv_file))

    assert result is False
    assert not csv_file.exists()


@pytest.mark.asyncio
async def test_get_message_count_supports_direct_user_dialog(
    mock_telegram_client, mock_user
):
    """Direct dialog count не должен отбрасываться из-за User entity."""
    mock_telegram_client.get_entity.return_value = mock_user
    mock_telegram_client.return_value = SimpleNamespace(count=42)

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_message_count("test_user")

    assert result == 42


@pytest.mark.asyncio
async def test_get_messages_supports_direct_user_dialog(
    mock_telegram_client, mock_user
):
    """Direct dialog history должен читаться тем же get_messages path."""
    from tests.conftest import AsyncIteratorMock

    mock_telegram_client.get_entity.return_value = mock_user
    mock_telegram_client.iter_messages.return_value = AsyncIteratorMock(
        [
            SimpleNamespace(
                id=1,
                date=datetime(2026, 4, 2, 10, 30, 0),
                from_id=SimpleNamespace(user_id=mock_user.id),
                message="hello",
                media=None,
                fwd_from=None,
                forward=None,
                reply_to=None,
                views=None,
                forwards=None,
                is_pinned=False,
            )
        ]
    )

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_messages("test_user", limit=10)

    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["text"] == "hello"
    mock_telegram_client.iter_messages.assert_called_once_with(
        mock_user,
        limit=10,
        min_id=0,
        reverse=False,
    )


@pytest.mark.asyncio
async def test_get_messages_since_stops_at_cutoff(mock_telegram_client, mock_user):
    """Since-export читает newest->oldest и останавливается на cutoff."""
    from tests.conftest import AsyncIteratorMock

    mock_telegram_client.get_entity.return_value = mock_user
    mock_telegram_client.iter_messages.return_value = AsyncIteratorMock(
        [
            SimpleNamespace(
                id=3,
                date=datetime(2026, 4, 2, 10, 30, 0),
                from_id=SimpleNamespace(user_id=mock_user.id),
                message="recent message",
                media=None,
                fwd_from=None,
                forward=None,
                reply_to=None,
                views=None,
                forwards=None,
                is_pinned=False,
                file=None,
            ),
            SimpleNamespace(
                id=2,
                date=datetime(2020, 12, 31, 23, 59, 0),
                from_id=SimpleNamespace(user_id=mock_user.id),
                message="old",
                media=None,
                fwd_from=None,
                forward=None,
                reply_to=None,
                views=None,
                forwards=None,
                is_pinned=False,
                file=None,
            ),
        ]
    )

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_messages_since("test_user", "2021-01-01")

    assert len(result) == 1
    assert result[0]["id"] == 3


@pytest.mark.asyncio
async def test_leave_dialog_dry_run_channel(mock_telegram_client, mock_channel):
    """Dry-run leave должен резолвить канал и не делать write request."""
    mock_channel.broadcast = True
    mock_telegram_client.get_entity.return_value = mock_channel

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.leave_dialog("testgroup", dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["action"] == "leave_dialog"
    assert result["dialog_type"] == "channel"
    assert mock_telegram_client.await_count == 0


@pytest.mark.asyncio
async def test_leave_dialog_blocks_direct_user(mock_telegram_client, mock_user):
    """Direct user dialogs cannot be left through channel leave action."""
    mock_telegram_client.get_entity.return_value = mock_user

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.leave_dialog("test_user", dry_run=True)

    assert result["success"] is False
    assert "Direct user dialogs" in result["error"]


@pytest.mark.asyncio
async def test_add_member_to_group_dry_run(
    mock_telegram_client, mock_channel, mock_user
):
    """Dry-run добавления участника не должен делать write-операцию."""
    mock_telegram_client.get_entity.side_effect = [mock_channel, mock_user]

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.add_member_to_group(
        "testgroup", "test_user", dry_run=True
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["action"] == "add_member"
    assert result["user_id"] == mock_user.id


@pytest.mark.asyncio
async def test_set_channel_comments_join_requirement_dry_run(
    mock_telegram_client, mock_channel
):
    """Dry-run должен находить linked discussion group и не делать write."""
    linked_group = MagicMock(spec=Channel)
    linked_group.id = 5023804528
    linked_group.title = "Matskevich comments"
    linked_group.username = "matskevich_chat"
    linked_group.join_to_send = True

    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.side_effect = [
        SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=linked_group.id),
            chats=[linked_group],
        )
    ]

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.set_channel_comments_join_requirement(
        "testgroup",
        join_required=False,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["linked_group_id"] == linked_group.id
    assert result["linked_group_target"] == "@matskevich_chat"
    assert result["current_join_required"] is True


@pytest.mark.asyncio
async def test_create_private_group_dry_run_resolves_invitees(
    mock_telegram_client, mock_user
):
    """Dry-run создания группы резолвит invitees и не делает write."""
    mock_telegram_client.get_entity.return_value = mock_user

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.create_private_group(
        "email critical",
        users=["@example_bot"],
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["title"] == "email critical"
    assert result["invitees"][0]["username"] == "test_user"
    assert mock_telegram_client.await_count == 0


@pytest.mark.asyncio
async def test_create_private_group_executes_create_and_invite(
    mock_telegram_client, mock_user
):
    """Execute path создает basic private group с initial invitee."""
    mock_chat = MagicMock(spec=Chat)
    mock_chat.id = -4000000000
    mock_chat.title = "email critical"
    mock_chat.participants_count = 2
    mock_telegram_client.get_entity.return_value = mock_user
    mock_telegram_client.side_effect = [
        SimpleNamespace(chats=[mock_chat]),
    ]

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.create_private_group(
        "email critical",
        users=["@example_bot"],
        dry_run=False,
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["group_id"] == mock_chat.id
    assert result["invites"][0]["success"] is True
    assert result["invites"][0]["created_with_group"] is True
    assert mock_telegram_client.await_count == 1
    create_request = mock_telegram_client.await_args_list[0].args[0]
    assert type(create_request).__name__ == "CreateChatRequest"
    assert create_request.title == "email critical"


@pytest.mark.asyncio
async def test_create_private_group_falls_back_to_dialog_title(
    mock_telegram_client, mock_user
):
    """Если Telegram Updates не содержит chat entity, ищем созданную группу по title."""
    from tests.conftest import AsyncIteratorMock

    mock_chat = MagicMock(spec=Chat)
    mock_chat.id = -4000000000
    mock_chat.title = "email critical"
    mock_chat.participants_count = 2
    mock_telegram_client.get_entity.return_value = mock_user
    mock_telegram_client.side_effect = [
        SimpleNamespace(chats=[]),
    ]
    mock_telegram_client.iter_dialogs.return_value = AsyncIteratorMock(
        [
            SimpleNamespace(
                entity=mock_chat, title="email critical", name="email critical"
            )
        ]
    )

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.create_private_group(
        "email critical",
        users=["@example_bot"],
        dry_run=False,
    )

    assert result["success"] is True
    assert result["group_id"] == mock_chat.id


@pytest.mark.asyncio
async def test_set_channel_comments_join_requirement_executes_toggle(
    mock_telegram_client, mock_channel
):
    """Execute path должен дернуть ToggleJoinToSendRequest для linked group."""
    linked_group = MagicMock(spec=Channel)
    linked_group.id = 5023804528
    linked_group.title = "Matskevich comments"
    linked_group.username = None
    linked_group.join_to_send = True

    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.side_effect = [
        SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=linked_group.id),
            chats=[linked_group],
        ),
        MagicMock(),
    ]

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.set_channel_comments_join_requirement(
        "testgroup",
        join_required=False,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["current_join_required"] is False
    assert mock_telegram_client.await_count == 2
    toggle_request = mock_telegram_client.await_args_list[1].args[0]
    assert type(toggle_request).__name__ == "ToggleJoinToSendRequest"
    assert toggle_request.enabled is False


@pytest.mark.asyncio
async def test_add_member_to_group_already_member(
    mock_telegram_client, mock_channel, mock_user
):
    """Если пользователь уже в группе, операция считается идемпотентно успешной."""
    mock_telegram_client.get_entity.side_effect = [mock_channel, mock_user]
    mock_telegram_client.side_effect = UserAlreadyParticipantError(request=None)

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.add_member_to_group(
        "testgroup", "test_user", dry_run=False
    )

    assert result["success"] is True
    assert result["already_member"] is True


@pytest.mark.asyncio
async def test_remove_member_from_group_not_participant(
    mock_telegram_client, mock_channel, mock_user
):
    """Если пользователя нет в группе, remove считается идемпотентно успешным."""
    mock_telegram_client.get_entity.side_effect = [mock_channel, mock_user]
    mock_telegram_client.side_effect = UserNotParticipantError(request=None)

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.remove_member_from_group(
        "testgroup", "test_user", dry_run=False
    )

    assert result["success"] is True
    assert result["not_participant"] is True


@pytest.mark.asyncio
async def test_migrate_member_dry_run(mock_telegram_client, mock_channel, mock_user):
    """Dry-run миграции возвращает план add/remove без изменений в Telegram."""
    mock_telegram_client.get_entity.side_effect = [
        mock_channel,
        mock_user,  # add preview
        mock_channel,
        mock_user,  # remove preview
    ]

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.migrate_member(
        group_identifier="testgroup",
        old_user_identifier="old_user",
        new_user_identifier="test_user",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["action"] == "migrate_member"
    assert result["add_new_user"]["action"] == "add_member"
    assert result["remove_old_user"]["action"] == "remove_member"


@pytest.mark.asyncio
async def test_delete_messages_uses_api_quota_not_group_message(
    monkeypatch, mock_telegram_client, mock_channel
):
    """delete_messages is destructive, but it must not consume send-message quota."""
    mock_telegram_client.get_entity.return_value = mock_channel
    calls = []

    async def fake_safe_api_call(func, *args, **kwargs):
        calls.append(kwargs.get("operation_type"))
        return True

    monkeypatch.setattr(groups_module, "_safe_api_call", fake_safe_api_call)

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.delete_messages(
        str(mock_channel.id), [1, 2], dry_run=False
    )

    assert result["success"] is True
    assert "group_msg" not in calls
    assert calls[-1] == "api"


@pytest.mark.asyncio
async def test_send_file_success(mock_telegram_client, mock_channel):
    """send_file использует safe путь и возвращает успех."""
    mock_telegram_client.get_entity.return_value = mock_channel
    mock_telegram_client.send_file = AsyncMock(return_value=MagicMock())

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.send_file("testgroup", "/tmp/example.md", caption="")

    assert result is True
    mock_telegram_client.send_file.assert_called_once()


@pytest.mark.asyncio
async def test_send_file_invalid_target(mock_telegram_client):
    """При невалидной цели send_file возвращает False."""
    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.send_file("x", "/tmp/example.md")
    assert result is False
