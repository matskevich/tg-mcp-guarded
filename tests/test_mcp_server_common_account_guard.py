import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "testhash")

import mcp_server_common as common


class DummyClient:
    def __init__(self, username: str, authorized: bool = True):
        self._username = username
        self._authorized = authorized
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return self._authorized

    async def get_me(self):
        return SimpleNamespace(id=12345, username=self._username, first_name="Test")


@pytest.mark.asyncio
async def test_connect_client_fails_fast_on_expected_username_mismatch(monkeypatch):
    monkeypatch.setenv("TG_EXPECTED_USERNAME", "@example_account")
    ctx = common.MCPServerContext(allow_session_switch=False)
    client = DummyClient(username="other_user")

    with pytest.raises(RuntimeError, match="Session mismatch"):
        await ctx._connect_client(client, "test_session")

    assert client.disconnected is True


@pytest.mark.asyncio
async def test_connect_client_allows_expected_username_case_insensitive(monkeypatch):
    monkeypatch.setenv("TG_EXPECTED_USERNAME", "example_account")
    ctx = common.MCPServerContext(allow_session_switch=False)
    client = DummyClient(username="ExAmPlE_Account")

    manager = await ctx._connect_client(client, "test_session")

    assert manager is not None
    assert ctx.current_session == "test_session"


@pytest.mark.asyncio
async def test_auth_status_reports_mismatch_as_unauthorized(monkeypatch):
    monkeypatch.setenv("TG_EXPECTED_USERNAME", "example_account")
    monkeypatch.delenv("TG_SESSION_PATH", raising=False)

    ctx = common.MCPServerContext(allow_session_switch=False)
    ctx._client = DummyClient(username="another_user")

    payload = await ctx.auth_status()

    assert payload["authorized"] is False
    assert "Session mismatch" in payload.get("error", "")
    assert "session_path_status" in payload


def test_detect_declared_session_conflict_same_paths(monkeypatch, tmp_path):
    session_path = str((tmp_path / "example_account.session").resolve())
    monkeypatch.setenv("TG_READ_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_ACTIONS_SESSION_PATH", session_path)

    issue = common._detect_declared_session_conflict("read", session_path)

    assert issue is not None
    assert issue["read_session_path"] == session_path
    assert "database is locked" in issue["message"]


def test_context_fail_fast_on_same_session_conflict(monkeypatch, tmp_path):
    session_path = str((tmp_path / "example_account.session").resolve())
    monkeypatch.setenv("TG_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_READ_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_ACTIONS_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "fail")

    with pytest.raises(RuntimeError, match="database is locked"):
        common.MCPServerContext(allow_session_switch=False, server_profile="actions")


def test_detect_live_session_conflict_same_profile_same_session(monkeypatch, tmp_path):
    session_path = str((tmp_path / "example_account_ro.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    registry_file.write_text(
        (
            '{"claims": {'
            '"read:111": {'
            '"pid": 111, '
            '"profile": "read", '
            f'"session_path": "{session_path}", '
            '"updated_at": 1'
            "}"
            "}}"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "_is_pid_alive", lambda pid: int(pid) == 111)

    issue = common._detect_live_session_conflict(
        registry_file,
        server_profile="read",
        session_path=session_path,
        own_claim_id="read:222",
    )

    assert issue is not None
    assert issue["other_profile"] == "read"
    assert "Multiple live 'read' MCP processes" in issue["message"]


def test_session_path_status_reports_effective_runtime_copy(monkeypatch, tmp_path):
    session_path = str((tmp_path / "example_account_ro.session").resolve())
    runtime_path = str((tmp_path / "runtime" / "shadow.session").resolve())

    monkeypatch.setenv("TG_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "off")
    monkeypatch.setattr(
        common,
        "describe_session_target",
        lambda _: {
            "mode": "copy",
            "source_session_file": session_path,
            "effective_session_file": runtime_path,
            "runtime_dir": str((tmp_path / "runtime").resolve()),
        },
    )

    ctx = common.MCPServerContext(allow_session_switch=False, server_profile="read")
    payload = ctx.session_path_status()

    assert payload["session_path"] == session_path
    assert payload["effective_session_path"] == runtime_path
    assert payload["session_runtime_mode"] == "copy"
