#!/usr/bin/env python3
"""
📥 Безопасная выгрузка переписки группы для анализа

Использование:
    python examples/export_group_messages.py "shipyard cohort 1"
    python examples/export_group_messages.py -1001234567890
    python examples/export_group_messages.py @groupname
"""

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
import argparse

# Добавляем путь к пакетам
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = PROJECT_ROOT / "packages"
TG_CORE_DIR = PKG_DIR / "tg_core"
for p in (str(PKG_DIR), str(TG_CORE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tganalytics.infra.tele_client import get_client, get_client_for_session
from tganalytics.infra.limiter import get_rate_limiter, safe_call
from tganalytics.domain.groups import GroupManager


async def find_group_by_title(client, title: str) -> Optional[Any]:
    """Находит группу по названию через список диалогов"""
    async def search_dialogs():
        dialogs = []
        async for dialog in client.iter_dialogs():
            if dialog.title and title.lower() in dialog.title.lower():
                dialogs.append(dialog)
        return dialogs
    
    dialogs = await safe_call(search_dialogs, operation_type="api")
    
    if not dialogs:
        return None
    
    # Если несколько совпадений - возвращаем первое точное или первое
    for dialog in dialogs:
        if dialog.title.lower() == title.lower():
            return dialog
    
    return dialogs[0]


async def export_messages_safe(
    client,
    group_identifier: str,
    output_file: str,
    limit: Optional[int] = None,
    min_id: int = 0
) -> Dict[str, Any]:
    """
    Безопасно выгружает сообщения группы используя существующую инфраструктуру
    
    Args:
        client: TelegramClient
        group_identifier: ID группы, username или название
        output_file: Путь к файлу для сохранения
        limit: Максимальное количество сообщений (None = все)
        min_id: Минимальный ID сообщения (для продолжения выгрузки)
    
    Returns:
        Словарь со статистикой выгрузки
    """
    print(f"🔍 Поиск группы: {group_identifier}")
    
    # Используем существующий GroupManager
    manager = GroupManager(client)
    group_info = await manager.get_group_info(group_identifier)
    
    if not group_info:
        # Пробуем найти по названию через список диалогов
        print(f"⚠️  Группа не найдена по ID/username, ищем по названию...")
        entity = await find_group_by_title(client, group_identifier)
        if not entity:
            raise ValueError(f"Группа '{group_identifier}' не найдена")
        group_id = entity.id
        group_title = entity.title
        print(f"✅ Найдена группа по названию: {group_title} (ID: {group_id})")
    else:
        group_id = group_info['id']
        group_title = group_info.get('title', 'Unknown')
    
    print(f"📥 Начинаем выгрузку сообщений...")
    if limit:
        print(f"   Лимит: {limit} сообщений")
    else:
        print(f"   Лимит: все сообщения")
    
    start_time = datetime.now()
    
    # Используем метод из GroupManager (вся антиспам защита уже внутри)
    # Передаем ID группы, а не название
    messages = await manager.get_messages(group_id, limit=limit, min_id=min_id)
    
    elapsed_time = (datetime.now() - start_time).total_seconds()
    last_message_id = messages[-1]['id'] if messages else None
    
    print(f"\n✅ Выгрузка завершена!")
    print(f"   Сообщений выгружено: {len(messages)}")
    print(f"   Время: {elapsed_time:.1f} секунд")
    if elapsed_time > 0:
        print(f"   Скорость: {len(messages) / elapsed_time:.1f} msg/sec")
    
    # Сохраняем в JSON
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    export_data = {
        'group': {
            'id': group_id,
            'title': group_title,
            'export_date': datetime.now().isoformat(),
            'total_messages': len(messages),
            'min_id': min_id,
            'last_message_id': last_message_id,
        },
        'messages': messages
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    
    print(f"💾 Данные сохранены в: {output_path}")
    print(f"   Размер файла: {output_path.stat().st_size / 1024 / 1024:.2f} MB")
    
    # Статистика антиспам
    limiter = get_rate_limiter()
    stats = limiter.get_stats()
    print(f"\n🛡️  Антиспам статистика:")
    print(f"   API вызовов: {stats['api_calls']}")
    print(f"   FLOOD_WAIT событий: {stats['flood_waits']}")
    
    return {
        'group_id': group_id,
        'group_title': group_title,
        'messages_count': len(messages),
        'output_file': str(output_path),
        'elapsed_seconds': elapsed_time,
        'api_calls': stats['api_calls'],
        'flood_waits': stats['flood_waits'],
    }


async def main():
    parser = argparse.ArgumentParser(
        description='Безопасная выгрузка переписки группы'
    )
    parser.add_argument(
        'group',
        help='ID группы, username или название (например: "shipyard cohort 1")'
    )
    parser.add_argument(
        '-o', '--output',
        default=None,
        help='Путь к файлу вывода (по умолчанию: data/export/messages_<group_id>.json)'
    )
    parser.add_argument(
        '-l', '--limit',
        type=int,
        default=None,
        help='Максимальное количество сообщений (по умолчанию: все)'
    )
    parser.add_argument(
        '--min-id',
        type=int,
        default=0,
        help='Минимальный ID сообщения (для продолжения выгрузки)'
    )
    parser.add_argument(
        '--session-name',
        default=None,
        help='Необязательная сессия (например: example_account). Если не указано — используется дефолтная из .env (SESSION_NAME).'
    )
    
    args = parser.parse_args()
    
    # Определяем путь к файлу вывода
    if args.output:
        output_file = args.output
    else:
        # Используем безопасное имя файла
        safe_name = args.group.replace(' ', '_').replace('/', '_')
        output_file = f"data/export/messages_{safe_name}.json"
    
    print("🚀 Запуск выгрузки переписки группы")
    print(f"   Группа: {args.group}")
    print(f"   Вывод: {output_file}")
    if args.limit:
        print(f"   Лимит: {args.limit} сообщений")
    print()
    
    # Подключаемся к Telegram
    if args.session_name:
        session_path = (PROJECT_ROOT / "data" / "sessions" / args.session_name).resolve()
        client = get_client_for_session(str(session_path))
    else:
        client = get_client()
    try:
        await client.start()
        
        # Выгружаем сообщения
        result = await export_messages_safe(
            client,
            args.group,
            output_file,
            limit=args.limit,
            min_id=args.min_id
        )
        
        print(f"\n🎉 Выгрузка успешно завершена!")
        print(f"   Группа: {result['group_title']}")
        print(f"   Сообщений: {result['messages_count']}")
        print(f"   Файл: {result['output_file']}")
        
    except KeyboardInterrupt:
        print("\n⚠️  Выгрузка прервана пользователем")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

