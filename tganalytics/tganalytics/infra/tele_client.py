from __future__ import annotations

import os
import asyncio
import atexit
import hashlib
import shutil
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from dotenv import load_dotenv
from telethon.tl.types import User
from .limiter import safe_call, get_rate_limiter
from .metrics import (
    increment_rate_limit_requests_total,
    increment_rate_limit_throttled_total,
    increment_flood_wait_events_total,
    observe_tele_call_latency_seconds,
)

load_dotenv()

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

# Безопасные пути для хранения данных (настраиваемые)
# Можно переопределить через SESSION_DIR, по умолчанию в data/sessions
SESSION_DIR = Path(os.getenv("SESSION_DIR", "data/sessions"))
SESSION_DIR.mkdir(parents=True, exist_ok=True)
SESSION_LOCK_MODE = os.getenv("TG_SESSION_LOCK_MODE", "shared").strip().lower()
RECEIVE_UPDATES = os.getenv("TG_RECEIVE_UPDATES", "0") == "1"

WRITE_GUARD_ENABLED = os.getenv("TG_BLOCK_DIRECT_TELETHON_WRITE", "1") == "1"
ALLOW_DIRECT_WRITE = os.getenv("TG_ALLOW_DIRECT_TELETHON_WRITE", "0") == "1"
ENFORCE_ACTION_PROCESS = os.getenv("TG_ENFORCE_ACTION_PROCESS", "1") == "1"
AUTH_BOOTSTRAP_ENABLED = os.getenv("TG_AUTH_BOOTSTRAP", "0") == "1"
ACTION_PROCESS_MARKER = os.getenv("TG_ACTION_PROCESS", "0") == "1"
WRITE_CONTEXT = os.getenv("TG_WRITE_CONTEXT", "").strip().lower()
WRITE_ALLOWED_CONTEXTS = {
    item.strip().lower()
    for item in os.getenv("TG_DIRECT_TELETHON_WRITE_ALLOWED_CONTEXTS", "actions_mcp").split(",")
    if item.strip()
}
SESSION_RUNTIME_MODE = os.getenv("TG_SESSION_RUNTIME_MODE", "direct").strip().lower()
if SESSION_RUNTIME_MODE in {"shadow", "shadow_copy"}:
    SESSION_RUNTIME_MODE = "copy"
if SESSION_RUNTIME_MODE not in {"direct", "copy"}:
    SESSION_RUNTIME_MODE = "direct"
SESSION_RUNTIME_PROFILE = (
    os.getenv("TG_SESSION_RUNTIME_PROFILE", "").strip().lower()
    or WRITE_CONTEXT
    or "default"
)
SESSION_RUNTIME_DIR = Path(
    os.getenv(
        "TG_SESSION_RUNTIME_DIR",
        str((SESSION_DIR / "runtime" / SESSION_RUNTIME_PROFILE).resolve()),
    )
)

READ_REQUEST_PREFIXES = (
    "Get",
    "Check",
    "Search",
    "Resolve",
    "Read",
    "Fetch",
    "Ping",
    "Help",
)

WRITE_REQUEST_PREFIXES = (
    "Send",
    "Edit",
    "Delete",
    "Forward",
    "Invite",
    "Add",
    "Join",
    "Leave",
    "Create",
    "Update",
    "Upload",
    "Import",
    "Export",
    "Pin",
    "Unpin",
    "Set",
    "Start",
    "Stop",
    "Save",
    "Install",
    "Uninstall",
    "Report",
    "Block",
    "Unblock",
    "Kick",
    "Ban",
    "Unban",
)

# Explicit auth bootstrap allowlist. Enabled only with TG_AUTH_BOOTSTRAP=1.
# This unlocks Telegram login flows without enabling general write operations.
AUTH_BOOTSTRAP_ALLOWED_REQUESTS = {
    "SendCodeRequest",
    "ResendCodeRequest",
    "SignInRequest",
    "CheckPasswordRequest",
    "GetPasswordRequest",
    "ExportLoginTokenRequest",
    "ImportLoginTokenRequest",
    "AcceptLoginTokenRequest",
}

# Усиление прав доступа для каталога/файлов сессии
def _harden_session_storage(directory: Path, session_file: Path) -> None:
    try:
        # Каталог только для владельца: 700
        current_mode = directory.stat().st_mode & 0o777
        if current_mode != 0o700:
            directory.chmod(0o700)
    except Exception:
        pass
    try:
        if session_file.exists():
            # Файл только для владельца: 600
            file_mode = session_file.stat().st_mode & 0o777
            if file_mode != 0o600:
                session_file.chmod(0o600)
    except Exception:
        pass

def _read_secret_from_command(env_var: str) -> str:
    cmd = os.getenv(env_var, "").strip()
    if not cmd:
        return ""
    try:
        parts = shlex.split(cmd)
    except Exception:
        return ""
    if not parts:
        return ""
    try:
        proc = subprocess.run(
            parts,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _read_secret_from_keychain(service: str, account: str) -> str:
    if not service or not account:
        return ""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _load_api_credentials() -> tuple[str, str]:
    provider = os.getenv("TG_SECRET_PROVIDER", "").strip().lower()
    if not provider and os.getenv("TG_USE_KEYCHAIN", "0") == "1":
        provider = "keychain"
    if not provider:
        provider = "env"

    raw_api_id = os.getenv("TG_API_ID", "").strip()
    raw_api_hash = os.getenv("TG_API_HASH", "").strip()

    if provider == "keychain":
        service = os.getenv("TG_KEYCHAIN_SERVICE", "tg-mcp").strip()
        id_account = os.getenv("TG_KEYCHAIN_ACCOUNT_API_ID", "TG_API_ID").strip()
        hash_account = os.getenv("TG_KEYCHAIN_ACCOUNT_API_HASH", "TG_API_HASH").strip()
        if not raw_api_id:
            raw_api_id = _read_secret_from_keychain(service, id_account)
        if not raw_api_hash:
            raw_api_hash = _read_secret_from_keychain(service, hash_account)
    elif provider == "command":
        if not raw_api_id:
            raw_api_id = _read_secret_from_command("TG_SECRET_CMD_API_ID")
        if not raw_api_hash:
            raw_api_hash = _read_secret_from_command("TG_SECRET_CMD_API_HASH")

    return raw_api_id, raw_api_hash


raw_api_id, api_hash = _load_api_credentials()
try:
    api_id = int(raw_api_id) if raw_api_id else 0
except ValueError:
    api_id = 0

session_name = os.getenv("SESSION_NAME", "example_session")
session_path = str(SESSION_DIR / session_name)

# Проверка конфигурации
if not api_id or not api_hash:
    raise ValueError(
        "❌ Missing TG API credentials. Set TG_API_ID/TG_API_HASH in env, "
        "or configure TG_SECRET_PROVIDER=keychain|command."
    )

_client = None
_clients_by_path = {}
_session_lock_fds = {}
_runtime_session_files = set()


def _normalize_session_file_path(path: Path) -> Path:
    """Return explicit *.session path for a Telethon session name/path."""
    if path.suffix == ".session":
        return path
    return path.with_suffix(".session")


def _runtime_safe_component(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned.strip("_") or "session"


def _runtime_session_file(source_session_file: Path) -> Path:
    source = _normalize_session_file_path(source_session_file).expanduser().resolve()
    digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:10]
    stem = _runtime_safe_component(source.stem)
    return (SESSION_RUNTIME_DIR / f"{stem}__{digest}__pid{os.getpid()}.session").resolve()


def describe_session_target(session_file: str | Path) -> dict[str, str]:
    """Describe canonical vs effective session file for the current process."""
    source = _normalize_session_file_path(Path(session_file)).expanduser().resolve()
    effective = source
    if SESSION_RUNTIME_MODE == "copy" and source.exists():
        effective = _runtime_session_file(source)
    return {
        "mode": SESSION_RUNTIME_MODE,
        "source_session_file": str(source),
        "effective_session_file": str(effective),
        "runtime_dir": str(SESSION_RUNTIME_DIR.resolve()),
    }


def _copy_session_sidecars(source: Path, target: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        source_companion = Path(f"{source}{suffix}")
        target_companion = Path(f"{target}{suffix}")
        if source_companion.exists():
            shutil.copy2(source_companion, target_companion)


def _prepare_runtime_session_copy(source_session_file: Path, effective_session_file: Path) -> None:
    if source_session_file == effective_session_file or not source_session_file.exists():
        return

    SESSION_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    target_key = str(effective_session_file.resolve())
    if target_key in _runtime_session_files and effective_session_file.exists():
        return

    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{effective_session_file}{suffix}")
        if candidate.exists():
            candidate.unlink()

    try:
        with sqlite3.connect(
            f"file:{source_session_file.resolve()}?mode=ro",
            uri=True,
            timeout=30,
        ) as source_conn:
            with sqlite3.connect(str(effective_session_file), timeout=30) as target_conn:
                source_conn.backup(target_conn)
                target_conn.commit()
    except Exception:
        shutil.copy2(source_session_file, effective_session_file)
        _copy_session_sidecars(source_session_file, effective_session_file)

    _runtime_session_files.add(target_key)


def _cleanup_runtime_session_files() -> None:
    for runtime_session in list(_runtime_session_files):
        runtime_path = Path(runtime_session)
        for suffix in ("", "-journal", "-wal", "-shm", ".lock"):
            candidate = (
                runtime_path.with_suffix(runtime_path.suffix + suffix)
                if suffix == ".lock"
                else Path(f"{runtime_path}{suffix}")
            )
            if not candidate.exists():
                continue
            try:
                candidate.unlink()
            except Exception:
                pass
        _runtime_session_files.discard(runtime_session)


def _is_direct_write_allowed() -> bool:
    """Whether direct Telegram write methods are allowed in the current process."""
    if not WRITE_GUARD_ENABLED:
        return True
    if ENFORCE_ACTION_PROCESS and not _is_actions_process():
        return False
    if ALLOW_DIRECT_WRITE:
        return True
    return bool(WRITE_CONTEXT and WRITE_CONTEXT in WRITE_ALLOWED_CONTEXTS)


def _is_actions_process() -> bool:
    """Detect whether current process is Action MCP entrypoint."""
    if ACTION_PROCESS_MARKER:
        return True

    argv0 = Path(sys.argv[0]).name.lower() if sys.argv else ""
    return argv0 in {"mcp_server_actions.py", "tganalytics/mcp_server_actions.py"}


def _is_telethon_write_request(request: object) -> bool:
    """Best-effort detection of MTProto write requests passed via client(...)."""
    if request is None:
        return False

    request_cls = request.__class__
    module = getattr(request_cls, "__module__", "")
    if "telethon.tl.functions" not in module:
        return False

    name = getattr(request_cls, "__name__", "")
    if AUTH_BOOTSTRAP_ENABLED and name in AUTH_BOOTSTRAP_ALLOWED_REQUESTS:
        return False
    if any(name.startswith(prefix) for prefix in READ_REQUEST_PREFIXES):
        return False
    if any(name.startswith(prefix) for prefix in WRITE_REQUEST_PREFIXES):
        return True
    return False


def _contains_telethon_write_request(request: object) -> bool:
    """Support batches of requests passed to client(...)."""
    if isinstance(request, (list, tuple, set)):
        return any(_is_telethon_write_request(item) for item in request)
    return _is_telethon_write_request(request)


def _raise_write_guard_error(method_name: str) -> None:
    raise PermissionError(
        f"Direct Telegram write '{method_name}' is blocked by default. "
        "Use tgmcp-actions (Action MCP) with confirm=true and allowlist. "
        "For session bootstrap only, set TG_AUTH_BOOTSTRAP=1."
    )


class GuardedTelegramClient(TelegramClient):
    """TelegramClient with default-deny write guard outside Action MCP."""

    async def __call__(self, request, *args, **kwargs):
        if _contains_telethon_write_request(request) and not _is_direct_write_allowed():
            _raise_write_guard_error(request.__class__.__name__)
        return await super().__call__(request, *args, **kwargs)

    async def send_message(self, *args, **kwargs):
        if not _is_direct_write_allowed():
            _raise_write_guard_error("send_message")
        return await super().send_message(*args, **kwargs)

    async def send_file(self, *args, **kwargs):
        if not _is_direct_write_allowed():
            _raise_write_guard_error("send_file")
        return await super().send_file(*args, **kwargs)

    async def delete_messages(self, *args, **kwargs):
        if not _is_direct_write_allowed():
            _raise_write_guard_error("delete_messages")
        return await super().delete_messages(*args, **kwargs)

    async def edit_message(self, *args, **kwargs):
        if not _is_direct_write_allowed():
            _raise_write_guard_error("edit_message")
        return await super().edit_message(*args, **kwargs)

    async def forward_messages(self, *args, **kwargs):
        if not _is_direct_write_allowed():
            _raise_write_guard_error("forward_messages")
        return await super().forward_messages(*args, **kwargs)


def _release_session_locks() -> None:
    """Release all acquired lock file descriptors on process exit."""
    for key, fd in list(_session_lock_fds.items()):
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        _session_lock_fds.pop(key, None)


def _acquire_session_lock(session_file: Path) -> None:
    """Acquire exclusive session lock only when TG_SESSION_LOCK_MODE=exclusive.

    Modes:
    - shared/off (default): no lock, allows concurrent use across projects.
    - exclusive: one process per session file.
    """
    if SESSION_LOCK_MODE in ("shared", "off", ""):
        return
    if SESSION_LOCK_MODE != "exclusive":
        return
    if fcntl is None:  # pragma: no cover
        return

    normalized = _normalize_session_file_path(session_file)
    normalized.parent.mkdir(parents=True, exist_ok=True)

    lock_file = normalized.with_suffix(normalized.suffix + ".lock")
    lock_path = lock_file.resolve()
    key = str(lock_path)
    if key in _session_lock_fds:
        return

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            f"Telegram session '{normalized}' is already in use by another process. "
            "Use a separate session or set TG_SESSION_LOCK_MODE=shared."
        )

    _session_lock_fds[key] = fd


atexit.register(_release_session_locks)
atexit.register(_cleanup_runtime_session_files)

def get_client():
    global _client
    if _client is None:
        target = describe_session_target(session_path)
        source_session_file = Path(target["source_session_file"])
        session_file = Path(target["effective_session_file"])
        _prepare_runtime_session_copy(source_session_file, session_file)
        _acquire_session_lock(session_file)
        # Усиливаем права хранилища перед созданием клиента
        _harden_session_storage(session_file.parent, session_file)
        runtime_session_name = (
            str(session_file.with_suffix("")) if session_file.suffix == ".session" else str(session_file)
        )
        _client = GuardedTelegramClient(
            runtime_session_name,
            api_id,
            api_hash,
            receive_updates=RECEIVE_UPDATES,
        )
    return _client

def get_client_for_session(custom_session_file_path: str):
    """Возвращает TelegramClient для указанного файла сессии.

    Не влияет на глобальный клиент; кеширует клиентов по полному пути.
    """
    if not custom_session_file_path:
        return get_client()
    target = describe_session_target(custom_session_file_path)
    source_session_file = Path(target["source_session_file"])
    normalized_session_file = Path(target["effective_session_file"])
    _prepare_runtime_session_copy(source_session_file, normalized_session_file)
    session_dir = normalized_session_file.parent
    session_dir.mkdir(parents=True, exist_ok=True)
    _acquire_session_lock(normalized_session_file)
    # Усиливаем права
    _harden_session_storage(session_dir, normalized_session_file)
    resolved = normalized_session_file.resolve()
    key = str(resolved)
    client = _clients_by_path.get(key)
    if client is None:
        # Telethon appends .session automatically — strip it to avoid double extension
        session_name = str(resolved.with_suffix("")) if resolved.suffix == ".session" else str(resolved)
        client = GuardedTelegramClient(
            session_name,
            api_id,
            api_hash,
            receive_updates=RECEIVE_UPDATES,
        )
        _clients_by_path[key] = client
    return client

async def test_connection():
    """Тестирует подключение к Telegram API с anti-spam защитой"""
    try:
        client = get_client()
        await client.start()
        
        # Используем safe_call для get_me() и метрики
        import time
        start = time.perf_counter()
        try:
            increment_rate_limit_requests_total()
            me = await safe_call(client.get_me, operation_type="api")
        except Exception as e:
            # Простейшая эвристика для FLOOD_WAIT
            if hasattr(e, "seconds"):
                increment_flood_wait_events_total()
            raise
        finally:
            observe_tele_call_latency_seconds(time.perf_counter() - start)
        print(f"✅ Подключение успешно: {me.username} (ID: {me.id})")
        
        # Показываем статистику anti-spam системы
        limiter = get_rate_limiter()
        stats = limiter.get_stats()
        print(f"🛡️  Anti-spam статус: API calls: {stats['api_calls']}, RPS: {stats['current_rps']}")
        
        await client.disconnect()
        # Усиливаем права после возможного создания/обновления файла сессии
        _harden_session_storage(SESSION_DIR, _normalize_session_file_path(Path(session_path)))
        return True
    except SessionPasswordNeededError:
        print("❌ Требуется двухфакторная аутентификация")
        return False
    except PhoneCodeInvalidError:
        print("❌ Неверный код подтверждения")
        return False
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(test_connection())
