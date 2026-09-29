"""Shared state/helpers for tg-mcp MCP servers."""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from typing import Any
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.environ.get("TG_ENV_FILE") or None)

from mcp_actions_state import load_json_dict, update_json_dict
from tganalytics.domain.groups import GroupManager
from tganalytics.infra.paths import resolve_state_path
from tganalytics.infra.tele_client import describe_session_target, get_client, get_client_for_session


def _expected_username() -> str:
    raw = os.environ.get("TG_EXPECTED_USERNAME", "").strip().lstrip("@")
    return raw.lower()


def _build_session_mismatch_error(expected_username: str, actual_username: str | None, account_id: int | None) -> str:
    expected = f"@{expected_username}"
    actual_clean = (actual_username or "").strip()
    actual = f"@{actual_clean}" if actual_clean else "<no_username>"
    return (
        f"Session mismatch: expected account {expected}, got {actual} (id={account_id}). "
        "Set TG_SESSION_PATH to the correct session and restart MCP."
    )


def _validate_expected_account(me: Any) -> str | None:
    expected = _expected_username()
    if not expected:
        return None

    actual_username = (getattr(me, "username", None) or "").strip().lower()
    actual_id = getattr(me, "id", None)
    if actual_username != expected:
        return _build_session_mismatch_error(expected, getattr(me, "username", None), actual_id)
    return None


def _resolve_session_path(raw_path: str) -> str:
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    return str(Path(raw).expanduser().resolve())


def _normalize_session_conflict_mode(raw_mode: str) -> str:
    mode = str(raw_mode or "").strip().lower()
    if mode in {"off", "warn", "fail"}:
        return mode
    return "warn"


def _session_conflict_registry_file() -> Path:
    return resolve_state_path(
        os.environ.get("TG_SESSION_CONFLICT_REGISTRY_FILE", ""),
        "anti_spam",
        "session_registry.json",
    )


def _is_pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True
    return True


def _ps_field(pid: Any, field: str) -> str:
    try:
        value = int(pid)
    except Exception:
        return ""
    if value <= 0:
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-o", field, "-p", str(value)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _process_fingerprint(pid: Any) -> str:
    """Return a start-time marker for pid, or "" when it cannot be read.

    A bare pid is not a process identity: the OS recycles pids, so a stale claim
    can point at an unrelated live process and block the server forever.
    """
    return _ps_field(pid, "lstart=")


def _process_command(pid: Any) -> str:
    """Return the command line behind pid, or "" when it cannot be read."""
    return _ps_field(pid, "command=")


def _looks_like_mcp_process(pid: Any) -> bool:
    command = _process_command(pid)
    if not command:
        return True
    return "mcp_server_" in command


def _is_claim_owner_alive(claim: dict[str, Any]) -> bool:
    """True when the process that wrote the claim is still the process behind its pid."""
    pid = claim.get("pid")
    if not _is_pid_alive(pid):
        return False
    recorded = str(claim.get("fingerprint") or "").strip()
    if not recorded:
        # Claim predates fingerprints: the pid alone proves nothing, so at least
        # require that it still belongs to an MCP server process.
        return _looks_like_mcp_process(pid)
    current = _process_fingerprint(pid)
    if not current:
        return True
    return current == recorded


def _declared_session_paths(server_profile: str, session_path: str) -> tuple[str, str]:
    read_path = _resolve_session_path(os.environ.get("TG_READ_SESSION_PATH", ""))
    actions_path = _resolve_session_path(os.environ.get("TG_ACTIONS_SESSION_PATH", ""))
    if not read_path and server_profile == "read":
        read_path = session_path
    if not actions_path and server_profile == "actions":
        actions_path = session_path
    return read_path, actions_path


def _build_declared_session_conflict_message(session_path: str) -> str:
    return (
        f"Read/Actions share one Telegram session file: {session_path}. "
        "Concurrent Telethon read+write processes can hit sqlite 'database is locked'. "
        "Prefer separate sessions by default: read -> *_ro.session, actions -> write session."
    )


def _describe_claim_owner(pid: Any) -> str:
    """Name the process holding a session, so the failure says who to go stop."""
    command = _process_command(pid)
    if command:
        return f"pid {pid}: {command}"
    return f"pid {pid}"


def _build_live_session_conflict_message(
    session_path: str,
    *,
    server_profile: str,
    other_profile: str,
    other_pid: Any = None,
) -> str:
    owner = f" Held by {_describe_claim_owner(other_pid)}." if other_pid else ""
    if other_profile == server_profile:
        return (
            f"Multiple live '{server_profile}' MCP processes share one Telegram session file: {session_path}. "
            "This is known to cause sqlite 'database is locked' under concurrent MCP traffic."
            f"{owner} "
            "Use a dedicated session file per MCP client or stop the duplicate process."
        )
    return (
        f"Read/Actions share one live Telegram session file: {session_path}. "
        "This is known to cause sqlite 'database is locked' under concurrent MCP traffic."
        f"{owner} "
        "Split sessions: read -> *_ro.session, actions -> write session."
    )


def _detect_declared_session_conflict(server_profile: str, session_path: str) -> dict[str, Any] | None:
    read_path, actions_path = _declared_session_paths(server_profile, session_path)
    if not read_path or not actions_path or read_path != actions_path:
        return None
    return {
        "kind": "same_session_path",
        "source": "declared_paths",
        "message": _build_declared_session_conflict_message(read_path),
        "read_session_path": read_path,
        "actions_session_path": actions_path,
    }


def _idle_takeover_sec() -> int:
    """Seconds an owner may sit idle before another process may take the session (0 = never)."""
    try:
        return max(0, int(os.environ.get("TG_SESSION_IDLE_TAKEOVER_SEC", "300")))
    except ValueError:
        return 300


def _is_claim_idle(claim: dict[str, Any]) -> bool:
    """True when the owner has not touched the session for longer than the takeover window.

    An owner that is alive but idle should not keep everyone else locked out: it
    refreshes its claim on every operation, so a stale timestamp means it is done.
    """
    window = _idle_takeover_sec()
    if window <= 0:
        return False
    try:
        updated_at = int(claim.get("updated_at") or 0)
    except (TypeError, ValueError):
        return False
    if updated_at <= 0:
        return False
    return (int(time.time()) - updated_at) > window


def _find_conflicting_claim(
    claims: dict[str, Any],
    *,
    server_profile: str,
    session_path: str,
    own_claim_id: str,
    ignore_idle: bool = False,
) -> dict[str, Any] | None:
    for claim_id, value in claims.items():
        if claim_id == own_claim_id or not isinstance(value, dict):
            continue
        other_profile = str(value.get("profile") or "").strip().lower()
        other_session_path = _resolve_session_path(value.get("session_path", ""))
        if other_session_path != session_path:
            continue
        if not _is_claim_owner_alive(value):
            continue
        if ignore_idle and _is_claim_idle(value):
            continue
        return {
            "kind": "same_session_path",
            "source": "live_registry",
            "message": _build_live_session_conflict_message(
                session_path,
                server_profile=server_profile,
                other_profile=other_profile,
                other_pid=value.get("pid"),
            ),
            "read_session_path": session_path,
            "actions_session_path": session_path,
            "other_profile": other_profile,
            "other_pid": value.get("pid"),
        }
    return None


def _claim_session_path(
    registry_file: Path,
    *,
    server_profile: str,
    session_path: str,
    claim_id: str,
    exclusive: bool = False,
) -> dict[str, Any] | None:
    """Claim session_path and report any foreign live claim, under one registry lock.

    Scanning and registering must not be two separate locked steps: two servers
    starting at the same moment would both register first, then both see the other
    and both refuse to start, leaving nobody holding the session.

    With exclusive=True the claim is only written when the session is free, so the
    caller that gets None is the single owner until it releases.
    """
    if not session_path:
        return None

    claim = {
        "pid": os.getpid(),
        "profile": server_profile,
        "session_path": session_path,
        "fingerprint": _process_fingerprint(os.getpid()),
        "updated_at": int(time.time()),
    }

    def _mut(claims: dict[str, Any]) -> dict[str, Any] | None:
        for key in [
            key
            for key, value in claims.items()
            if key != claim_id and not (isinstance(value, dict) and _is_claim_owner_alive(value))
        ]:
            claims.pop(key, None)
        conflict = _find_conflicting_claim(
            claims,
            server_profile=server_profile,
            session_path=session_path,
            own_claim_id=claim_id,
            ignore_idle=exclusive,
        )
        if conflict is not None and exclusive:
            return conflict
        claims[claim_id] = claim
        return conflict

    return update_json_dict(registry_file, _mut, root_key="claims")


def _release_session_claim(registry_file: Path, claim_id: str) -> None:
    """Drop our claim so another process can take the session."""
    if not claim_id:
        return

    def _mut(claims: dict[str, Any]) -> None:
        claims.pop(claim_id, None)

    update_json_dict(registry_file, _mut, root_key="claims")


def _detect_live_session_conflict(
    registry_file: Path,
    *,
    server_profile: str,
    session_path: str,
    own_claim_id: str,
) -> dict[str, Any] | None:
    if not session_path:
        return None
    return _find_conflicting_claim(
        load_json_dict(registry_file, root_key="claims"),
        server_profile=server_profile,
        session_path=session_path,
        own_claim_id=own_claim_id,
    )


class MCPServerContext:
    """Shared runtime state for MCP servers.

    Keeps one active Telegram client/session per server process.
    """

    def __init__(
        self,
        sessions_dir: str | None = None,
        allow_session_switch: bool = True,
        server_profile: str = "read",
    ):
        self.sessions_dir = str(
            resolve_state_path(sessions_dir or os.environ.get("TG_SESSIONS_DIR", ""), "sessions")
        )
        self.allow_session_switch = allow_session_switch
        self.server_profile = server_profile

        self._client = None
        self._manager: GroupManager | None = None
        self._current_session: str | None = None
        self._current_session_path = ""
        self._current_effective_session_path = ""
        self._session_runtime_mode = "direct"
        self._session_conflict_mode = _normalize_session_conflict_mode(
            os.environ.get("TG_SESSION_PATH_CONFLICT_MODE", "warn")
        )
        self._session_conflict_registry_file = _session_conflict_registry_file()
        self._last_session_conflict_message: str | None = None
        self._session_conflict: dict[str, Any] | None = None
        self._session_claim_id: str | None = None
        self._registered_session_path: str | None = None
        self._takes_session_lazily = server_profile == "actions"

        self._set_current_session_path(os.environ.get("TG_SESSION_PATH", ""))
        self._enforce_session_path_policy()

    @property
    def current_session(self) -> str | None:
        return self._current_session

    @property
    def client(self) -> Any:
        return self._client

    def _set_current_session_path(self, session_path: str) -> None:
        resolved = _resolve_session_path(session_path)
        self._current_session_path = resolved
        self._current_effective_session_path = resolved
        self._session_runtime_mode = "direct"
        if not resolved:
            return

        target = describe_session_target(resolved)
        self._current_session_path = target["source_session_file"]
        self._current_effective_session_path = target["effective_session_file"]
        self._session_runtime_mode = target["mode"]

    def _claim_session(self, session_path: str) -> dict[str, Any] | None:
        if self._session_conflict_mode == "off" or not session_path:
            return None
        claim_id = self._session_claim_id or f"{self.server_profile}:{os.getpid()}"
        issue = _claim_session_path(
            self._session_conflict_registry_file,
            server_profile=self.server_profile,
            session_path=session_path,
            claim_id=claim_id,
        )
        self._session_claim_id = claim_id
        self._registered_session_path = session_path
        return issue

    def _enforce_session_path_policy(self) -> dict[str, Any] | None:
        session_path = self._current_session_path
        effective_session_path = self._current_effective_session_path or session_path
        issue = _detect_declared_session_conflict(self.server_profile, session_path)
        report_only = False
        if issue is None and effective_session_path and self._session_conflict_mode != "off":
            if self._takes_session_lazily:
                # Ownership is taken at connect time, not at import time: a server that
                # dies on startup takes its read tools down with it and leaves the user
                # with no MCP at all. Startup only reports who currently holds it.
                issue = _detect_live_session_conflict(
                    self._session_conflict_registry_file,
                    server_profile=self.server_profile,
                    session_path=effective_session_path,
                    own_claim_id=f"{self.server_profile}:{os.getpid()}",
                )
                report_only = issue is not None
            else:
                issue = self._claim_session(effective_session_path)

        self._session_conflict = issue
        if not issue or self._session_conflict_mode == "off":
            return issue

        message = issue["message"]
        if self._session_conflict_mode == "fail" and not report_only:
            raise RuntimeError(message)
        if message != self._last_session_conflict_message:
            print(f"[tg-mcp][session-warning] {message}", file=sys.stderr)
            self._last_session_conflict_message = message
        return issue

    def session_path_status(self) -> dict[str, Any]:
        issue = self._enforce_session_path_policy()
        read_path, actions_path = _declared_session_paths(self.server_profile, self._current_session_path)
        return {
            "server_profile": self.server_profile,
            "mode": self._session_conflict_mode,
            "session_path": self._current_session_path or None,
            "effective_session_path": self._current_effective_session_path or None,
            "session_runtime_mode": self._session_runtime_mode,
            "declared_read_session_path": read_path or None,
            "declared_actions_session_path": actions_path or None,
            "conflict": issue,
        }

    async def _connect_client(self, client, session_name: str) -> GroupManager:
        self._enforce_session_path_policy()
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                f"Session '{session_name}' is not authorized. "
                "Run create_telegram_session.py (or scripts/create_session_qr.py) to re-authenticate. "
                "Telegram login code usually arrives in-app (SentCodeTypeApp), not SMS."
            )

        me = await client.get_me()
        mismatch_error = _validate_expected_account(me)
        if mismatch_error:
            await client.disconnect()
            raise RuntimeError(mismatch_error)

        self._client = client
        self._current_session = session_name
        self._manager = GroupManager(client)
        return self._manager

    async def acquire_session(self) -> None:
        """Take exclusive ownership of the session file for the work about to happen."""
        session_path = self._current_effective_session_path or self._current_session_path
        if not self._takes_session_lazily or self._session_conflict_mode == "off" or not session_path:
            return

        # Claimed every time, not just the first: the timestamp is the heartbeat that
        # tells other processes we are still using the session rather than sitting idle.
        claim_id = f"{self.server_profile}:{os.getpid()}"
        issue = _claim_session_path(
            self._session_conflict_registry_file,
            server_profile=self.server_profile,
            session_path=session_path,
            claim_id=claim_id,
            exclusive=True,
        )
        if issue is not None:
            # We were evicted while idle; let go of the connection before reporting,
            # or we keep the sqlite file locked against the new owner.
            self._session_claim_id = None
            self._registered_session_path = None
            await self.release_session()
            self._session_conflict = issue
            raise RuntimeError(issue["message"])

        self._session_claim_id = claim_id
        self._registered_session_path = session_path
        self._session_conflict = None

    async def release_session(self) -> None:
        """Disconnect and hand the session back so another process can take it."""
        client = self._client
        self._client = None
        self._manager = None
        self._current_session = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if self._session_claim_id:
            _release_session_claim(self._session_conflict_registry_file, self._session_claim_id)
            self._session_claim_id = None
            self._registered_session_path = None

    async def get_manager(self) -> GroupManager:
        """Lazy-init manager and connect on the first call."""
        if self._manager is None:
            await self.acquire_session()
            session_path = os.environ.get("TG_SESSION_PATH", "").strip()
            if session_path:
                self._set_current_session_path(session_path)
                session_name = os.path.basename(session_path).replace(".session", "")
                client = get_client_for_session(session_path)
            else:
                session_name = os.environ.get("SESSION_NAME", "default")
                client = get_client()

            await self._connect_client(client, session_name)

        return self._manager

    async def list_sessions(self) -> dict[str, Any]:
        sessions = [
            os.path.basename(path).replace(".session", "")
            for path in glob.glob(os.path.join(self.sessions_dir, "*.session"))
        ]
        return {"sessions": sorted(sessions), "current": self._current_session}

    async def use_session(self, session_name: str) -> dict[str, Any]:
        if not self.allow_session_switch:
            return {
                "error": "Session switching is disabled. "
                "Set TG_ALLOW_SESSION_SWITCH=1 to enable tg_use_session."
            }

        path = os.path.join(self.sessions_dir, f"{session_name}.session")
        if not os.path.exists(path):
            return {"error": f"Session '{session_name}' not found"}

        if self._client is not None:
            await self._client.disconnect()

        try:
            self._set_current_session_path(path)
            self._enforce_session_path_policy()
            client = get_client_for_session(path)
            await self._connect_client(client, session_name)
            me = await self._client.get_me()
            return {"switched_to": session_name, "account": me.username or me.first_name}
        except RuntimeError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Failed to switch session: {exc}"}

    async def auth_status(self) -> dict[str, Any]:
        """Return authorization status for current/default Telegram session."""
        session_path = os.environ.get("TG_SESSION_PATH", "").strip()
        if session_path:
            resolved_path = str(Path(session_path).expanduser().resolve())
            self._set_current_session_path(resolved_path)
            self._enforce_session_path_policy()
            session_name = Path(session_path).name.replace(".session", "")
            client = self._client or get_client_for_session(session_path)
            is_transient = self._client is None
        else:
            resolved_path = ""
            session_name = os.environ.get("SESSION_NAME", "default")
            client = self._client or get_client()
            is_transient = self._client is None

        try:
            await client.connect()
            authorized = await client.is_user_authorized()
            payload: dict[str, Any] = {
                "authorized": bool(authorized),
                "session_name": session_name,
                "session_path": resolved_path or None,
                "session_path_status": self.session_path_status(),
            }
            if authorized:
                me = await client.get_me()
                payload["account"] = {
                    "id": getattr(me, "id", None),
                    "username": getattr(me, "username", None),
                    "first_name": getattr(me, "first_name", None),
                }
                mismatch_error = _validate_expected_account(me)
                if mismatch_error:
                    payload["authorized"] = False
                    payload["error"] = mismatch_error
            return payload
        except Exception as exc:
            return {
                "authorized": False,
                "session_name": session_name,
                "session_path": resolved_path or None,
                "session_path_status": self.session_path_status(),
                "error": str(exc),
            }
        finally:
            if is_transient:
                try:
                    await client.disconnect()
                except Exception:
                    pass
