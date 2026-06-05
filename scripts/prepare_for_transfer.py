#!/usr/bin/env python3
"""
Скрипт для подготовки проекта tg-mcp к передаче другому разработчику.
Автоматически очищает конфиденциальные данные и создает архив.
"""

import os
import sys
import shutil
import zipfile
from pathlib import Path
from datetime import datetime


def print_header():
    """Печатает заголовок скрипта"""
    print("🚀 tg-mcp - Подготовка к передаче проекта")
    print("=" * 50)


def check_git_status():
    """Проверяет статус git репозитория"""
    print("\n🔍 Проверка статуса git...")
    
    if not Path(".git").exists():
        print("⚠️  ВНИМАНИЕ: Проект не находится в git репозитории")
        return False
    
    # Проверка незакоммиченных изменений
    import subprocess
    try:
        result = subprocess.run(['git', 'status', '--porcelain'], 
                              capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            print("⚠️  ВНИМАНИЕ: Есть незакоммиченные изменения:")
            print(result.stdout)
            response = input("Продолжить? (y/N): ").lower()
            if response != 'y':
                return False
        else:
            print("✅ Git статус чистый")
    except FileNotFoundError:
        print("⚠️  Git не найден в системе")
        
    return True


def find_sensitive_files():
    """Находит конфиденциальные файлы"""
    print("\n🔍 Поиск конфиденциальных файлов...")
    
    sensitive_patterns = [
        ".env",
        "data/sessions/*.session",
        "data/export/*",
        "data/anti_spam/*",
        "data/logs/*",
        "*.pyc",
        "__pycache__",
        "venv/",
        ".DS_Store"
    ]
    
    found_files = []
    
    for pattern in sensitive_patterns:
        if "*" in pattern:
            # Обработка wildcard паттернов
            import glob
            matches = glob.glob(pattern, recursive=True)
            found_files.extend(matches)
        else:
            if Path(pattern).exists():
                found_files.append(pattern)
    
    if found_files:
        print("🔍 Найдены конфиденциальные файлы:")
        for file in found_files:
            print(f"   - {file}")
    else:
        print("✅ Конфиденциальные файлы не найдены")
    
    return found_files


def create_transfer_directory():
    """Создает директорию для передачи"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    transfer_dir = Path(f"../tg-mcp-transfer-{timestamp}")
    
    print(f"\n📁 Создание директории передачи: {transfer_dir}")
    
    if transfer_dir.exists():
        print(f"⚠️  Директория {transfer_dir} уже существует")
        response = input("Перезаписать? (y/N): ").lower()
        if response != 'y':
            return None
        shutil.rmtree(transfer_dir)
    
    transfer_dir.mkdir()
    return transfer_dir


def copy_safe_files(transfer_dir):
    """Копирует безопасные файлы в директорию передачи"""
    print(f"\n📋 Копирование файлов в {transfer_dir}...")
    
    # Файлы и папки для исключения
    exclude_patterns = {
        '.env',
        'data/sessions',
        'data/export', 
        'data/logs',
        'data/anti_spam',
        '__pycache__',
        '.git',
        'venv',
        '.DS_Store',
        '.pytest_cache'
    }
    
    def should_exclude(path):
        """Проверяет, нужно ли исключить файл/папку"""
        for pattern in exclude_patterns:
            if pattern in str(path):
                return True
        return False
    
    copied_count = 0
    skipped_count = 0
    
    for item in Path(".").rglob("*"):
        if should_exclude(item):
            skipped_count += 1
            continue
            
        relative_path = item.relative_to(".")
        target_path = transfer_dir / relative_path
        
        try:
            if item.is_file():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target_path)
                copied_count += 1
            elif item.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"⚠️  Ошибка копирования {item}: {e}")
    
    print(f"✅ Скопировано файлов: {copied_count}")
    print(f"⏭️  Пропущено файлов: {skipped_count}")


def create_clean_env_sample(transfer_dir):
    """Создает чистый .env.sample файл"""
    print("\n📝 Создание чистого .env.sample...")
    
    env_sample_content = """# Telegram API ключи (получить на https://my.telegram.org/apps)
TG_API_ID=
TG_API_HASH=
SESSION_NAME=example_session

# Anti-spam настройки
RATE_RPS=4                      # Запросов в секунду
MAX_DM_PER_DAY=20              # Максимум DM в сутки
MAX_JOINS_PER_DAY=20           # Максимум join/leave в сутки
MAX_GROUPS=200                 # Максимум групп для аккаунта

# Дополнительные настройки
LOG_LEVEL=INFO
SAFE_LOG_ENABLED=true
DATA_DIR=data
SESSIONS_DIR=data/sessions
EXPORT_DIR=data/export
ANTI_SPAM_DIR=data/anti_spam
LOGS_DIR=data/logs

# Безопасность
SESSION_PERMISSIONS=600
ENABLE_2FA_CHECK=true
CHECK_USERNAME=true
CHECK_PROFILE_PHOTO=true
CHECK_BIO=true
WARMUP_PERIOD_HOURS=24

# Разработка
DEBUG_MODE=false
TEST_MODE=false
DRY_RUN=false
"""
    
    env_file = transfer_dir / ".env.sample"
    with open(env_file, 'w', encoding='utf-8') as f:
        f.write(env_sample_content)
    
    print("✅ Создан чистый .env.sample")


def create_setup_script(transfer_dir):
    """Создает скрипт быстрой настройки"""
    print("\n📝 Создание скрипта быстрой настройки...")
    
    setup_script = """#!/bin/bash
# Скрипт быстрой настройки tg-mcp

echo "🚀 Быстрая настройка tg-mcp"
echo "================================"

# Создание виртуального окружения
echo "📦 Создание виртуального окружения..."
python3 -m venv venv

# Активация окружения
echo "🔧 Активация виртуального окружения..."
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    source venv/Scripts/activate
else
    source venv/bin/activate
fi

# Установка зависимостей
echo "📥 Установка зависимостей..."
pip install --upgrade pip
pip install -r requirements.txt

# Создание директорий
echo "📁 Создание необходимых директорий..."
mkdir -p data/sessions
mkdir -p data/export
mkdir -p data/anti_spam
mkdir -p data/logs

# Создание .env файла
echo "📝 Создание .env файла..."
if [ ! -f ".env" ]; then
    cp .env.sample .env
    echo "✅ Создан .env файл из шаблона"
    echo ""
    echo "⚠️  ВАЖНО: Отредактируйте .env файл и добавьте ваши Telegram API ключи:"
    echo "   1. Перейдите на https://my.telegram.org/apps"
    echo "   2. Создайте новое приложение"
    echo "   3. Скопируйте api_id и api_hash в .env файл"
    echo ""
else
    echo "ℹ️  .env файл уже существует"
fi

# Установка прав доступа
echo "🔒 Настройка прав доступа..."
chmod 600 data/sessions/* 2>/dev/null || true
chmod 700 data/sessions

echo ""
echo "✅ Настройка завершена!"
echo ""
echo "🎯 Следующие шаги:"
echo "   1. Отредактируйте .env файл (добавьте API ключи)"
echo "   2. Запустите: python3 src/infra/tele_client.py"
echo "   3. Протестируйте: python3 examples/list_my_chats.py"
echo ""
echo "📚 Документация: docs/TRANSFER_GUIDE.md"
"""
    
    setup_file = transfer_dir / "setup.sh"
    with open(setup_file, 'w', encoding='utf-8') as f:
        f.write(setup_script)
    
    # Делаем файл исполняемым
    os.chmod(setup_file, 0o755)
    
    print("✅ Создан скрипт setup.sh")


def create_archive(transfer_dir):
    """Создает ZIP архив"""
    print(f"\n📦 Создание ZIP архива...")
    
    archive_name = f"{transfer_dir.name}.zip"
    archive_path = transfer_dir.parent / archive_name
    
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in transfer_dir.rglob("*"):
            if file.is_file():
                arcname = file.relative_to(transfer_dir.parent)
                zipf.write(file, arcname)
    
    print(f"✅ Создан архив: {archive_path}")
    
    # Показываем размер архива
    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"📊 Размер архива: {size_mb:.1f} MB")
    
    return archive_path


def print_summary(transfer_dir, archive_path):
    """Печатает итоговую информацию"""
    print("\n" + "=" * 50)
    print("✅ ПОДГОТОВКА К ПЕРЕДАЧЕ ЗАВЕРШЕНА")
    print("=" * 50)
    print(f"📁 Директория: {transfer_dir}")
    print(f"📦 Архив: {archive_path}")
    print()
    print("📋 Что включено:")
    print("   ✅ Весь исходный код")
    print("   ✅ Документация (включая TRANSFER_GUIDE.md)")
    print("   ✅ Примеры использования")
    print("   ✅ Тесты") 
    print("   ✅ Скрипт быстрой настройки (setup.sh)")
    print("   ✅ Чистый .env.sample")
    print()
    print("🔒 Что исключено:")
    print("   ❌ .env файл (API ключи)")
    print("   ❌ Сессионные файлы Telegram")
    print("   ❌ Экспортированные данные")
    print("   ❌ Виртуальное окружение")
    print("   ❌ Cache файлы")
    print()
    print("🚀 Инструкции для друга:")
    print("   1. Распакуйте архив")
    print("   2. Запустите: bash setup.sh")
    print("   3. Читайте: docs/TRANSFER_GUIDE.md")
    print()


def main():
    """Основная функция"""
    try:
        print_header()
        
        # Проверка текущей директории
        if not Path("src").exists() or not Path("README.md").exists():
            print("❌ Ошибка: Запустите скрипт из корневой директории проекта tg-mcp")
            sys.exit(1)
        
        # Проверка git статуса
        if not check_git_status():
            print("❌ Отменено пользователем")
            sys.exit(1)
        
        # Поиск конфиденциальных файлов
        sensitive_files = find_sensitive_files()
        
        if sensitive_files:
            print("\n⚠️  Найдены конфиденциальные файлы. Они НЕ будут включены в передачу.")
            response = input("Продолжить? (y/N): ").lower()
            if response != 'y':
                print("❌ Отменено пользователем")
                sys.exit(1)
        
        # Создание директории передачи
        transfer_dir = create_transfer_directory()
        if not transfer_dir:
            print("❌ Отменено пользователем")
            sys.exit(1)
        
        # Копирование безопасных файлов
        copy_safe_files(transfer_dir)
        
        # Создание чистого .env.sample
        create_clean_env_sample(transfer_dir)
        
        # Создание скрипта настройки
        create_setup_script(transfer_dir)
        
        # Создание архива
        archive_path = create_archive(transfer_dir)
        
        # Итоговая информация
        print_summary(transfer_dir, archive_path)
        
        print("🎉 Проект готов к передаче!")
        
    except KeyboardInterrupt:
        print("\n❌ Прервано пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 