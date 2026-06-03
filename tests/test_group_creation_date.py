from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tganalytics.domain.groups import GroupManager


@pytest.fixture
def mock_first_message():
    """Мок первого сообщения с датой"""
    message = MagicMock()
    message.date = datetime(2024, 7, 29, 11, 58, 7)
    return message


@pytest.mark.asyncio
async def test_get_group_creation_date_with_id(
    mock_telegram_client, mock_channel, mock_first_message
):
    """Тест получения даты создания группы по ID"""
    mock_telegram_client.get_entity.return_value = mock_channel

    # Создаем правильный async generator для iter_messages
    async def mock_iter_messages(*args, **kwargs):
        yield mock_first_message

    mock_telegram_client.iter_messages = mock_iter_messages

    group_manager = GroupManager(mock_telegram_client)

    # Тестируем с числовым ID
    result = await group_manager.get_group_creation_date(-1002188344480)

    assert result is not None
    assert result.year == 2024
    assert result.month == 7
    assert result.day == 29


@pytest.mark.asyncio
async def test_get_group_creation_date_with_string_id(
    mock_telegram_client, mock_channel, mock_first_message
):
    """Тест получения даты создания группы по строковому ID"""
    mock_telegram_client.get_entity.return_value = mock_channel

    async def mock_iter_messages(*args, **kwargs):
        yield mock_first_message

    mock_telegram_client.iter_messages = mock_iter_messages

    group_manager = GroupManager(mock_telegram_client)

    # Тестируем со строковым ID
    result = await group_manager.get_group_creation_date("-1002188344480")

    assert result is not None
    assert result.year == 2024


@pytest.mark.asyncio
async def test_get_group_creation_date_with_username(
    mock_telegram_client, mock_channel, mock_first_message
):
    """Тест получения даты создания группы по username"""
    mock_telegram_client.get_entity.return_value = mock_channel

    async def mock_iter_messages(*args, **kwargs):
        yield mock_first_message

    mock_telegram_client.iter_messages = mock_iter_messages

    group_manager = GroupManager(mock_telegram_client)

    # Тестируем с username без @
    result = await group_manager.get_group_creation_date("testgroup")

    assert result is not None


@pytest.mark.asyncio
async def test_get_group_creation_date_resolves_dialog_title_with_spaces(
    mock_telegram_client, mock_channel, mock_first_message
):
    """Title с пробелами должен работать и для даты создания."""
    from tests.conftest import AsyncIteratorMock

    mock_channel.title = "attia project"
    mock_telegram_client.iter_dialogs = MagicMock(
        return_value=AsyncIteratorMock([SimpleNamespace(entity=mock_channel)])
    )

    async def mock_iter_messages(*args, **kwargs):
        yield mock_first_message

    mock_telegram_client.iter_messages = mock_iter_messages

    group_manager = GroupManager(mock_telegram_client)
    result = await group_manager.get_group_creation_date("attia project")

    assert result is not None
    assert result.year == 2024


@pytest.mark.asyncio
async def test_get_group_creation_date_no_messages(mock_telegram_client):
    """Тест случая когда в группе нет сообщений"""

    async def mock_iter_messages(*args, **kwargs):
        # Пустой async generator
        if False:
            yield  # недостижимый код для создания async generator

    mock_telegram_client.iter_messages = mock_iter_messages

    group_manager = GroupManager(mock_telegram_client)

    result = await group_manager.get_group_creation_date(-1002188344480)

    assert result is None


@pytest.mark.asyncio
async def test_get_group_creation_date_error(mock_telegram_client):
    """Тест обработки ошибок при получении даты создания"""

    # Настраиваем мок для выброса исключения
    async def mock_iter_messages_error(*args, **kwargs):
        if False:
            yield None
        raise Exception("Connection error")

    mock_telegram_client.iter_messages = mock_iter_messages_error

    group_manager = GroupManager(mock_telegram_client)

    result = await group_manager.get_group_creation_date(-1002188344480)

    assert result is None
