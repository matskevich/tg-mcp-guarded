import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def _write_registry(registry_file, claims):
    registry_file.write_text(json.dumps({"claims": claims}, ensure_ascii=False), encoding="utf-8")


def test_claim_session_path_ignores_recycled_pid(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _write_registry(
        registry_file,
        {
            "actions:111": {
                "pid": 111,
                "profile": "actions",
                "session_path": session_path,
                "fingerprint": "started-monday",
                "updated_at": 1,
            }
        },
    )
    monkeypatch.setattr(common, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(common, "_process_fingerprint", lambda pid: "started-friday")

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
    )

    assert issue is None
    claims = json.loads(registry_file.read_text(encoding="utf-8"))["claims"]
    assert "actions:111" not in claims
    assert claims["actions:222"]["fingerprint"] == "started-friday"


def test_claim_session_path_blocks_on_legacy_claim_without_fingerprint(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _write_registry(
        registry_file,
        {
            "actions:111": {
                "pid": 111,
                "profile": "actions",
                "session_path": session_path,
                "updated_at": 1,
            }
        },
    )
    monkeypatch.setattr(common, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(common, "_process_fingerprint", lambda pid: "started-friday")
    monkeypatch.setattr(common, "_process_command", lambda pid: "python tganalytics/mcp_server_actions.py")

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
    )

    assert issue is not None
    assert issue["other_pid"] == 111
    assert "Multiple live 'actions' MCP processes" in issue["message"]


def test_concurrent_claims_leave_exactly_one_session_owner(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    monkeypatch.setattr(common, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(common, "_process_fingerprint", lambda pid: "same-fingerprint")

    barrier = threading.Barrier(2)

    def claim(suffix):
        barrier.wait()
        return common._claim_session_path(
            registry_file,
            server_profile="actions",
            session_path=session_path,
            claim_id=f"actions:{suffix}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        issues = list(pool.map(claim, [901, 902]))

    assert sum(1 for issue in issues if issue is None) == 1
    assert sum(1 for issue in issues if issue is not None) == 1


def test_claim_session_path_ignores_legacy_claim_on_foreign_process(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _write_registry(
        registry_file,
        {
            "actions:794": {
                "pid": 794,
                "profile": "actions",
                "session_path": session_path,
                "updated_at": 1,
            }
        },
    )
    monkeypatch.setattr(common, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(common, "_process_fingerprint", lambda pid: "started-friday")
    monkeypatch.setattr(common, "_process_command", lambda pid: "/usr/local/bin/vpn_helper --daemon")

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
    )

    assert issue is None
    assert "actions:794" not in json.loads(registry_file.read_text(encoding="utf-8"))["claims"]


def test_live_conflict_message_names_the_owning_process(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _write_registry(
        registry_file,
        {
            "actions:4242": {
                "pid": 4242,
                "profile": "actions",
                "session_path": session_path,
                "fingerprint": "started-friday",
                "updated_at": 1,
            }
        },
    )
    monkeypatch.setattr(common, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(common, "_process_fingerprint", lambda pid: "started-friday")
    monkeypatch.setattr(common, "_process_command", lambda pid: "python tganalytics/mcp_server_actions.py")

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
    )

    assert issue is not None
    assert "pid 4242" in issue["message"]
    assert "mcp_server_actions.py" in issue["message"]


def _live(monkeypatch, fingerprint="started-friday", command="python tganalytics/mcp_server_actions.py"):
    monkeypatch.setattr(common, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(common, "_process_fingerprint", lambda pid: fingerprint)
    monkeypatch.setattr(common, "_process_command", lambda pid: command)


def test_exclusive_claim_does_not_register_when_session_is_taken(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _write_registry(
        registry_file,
        {
            "actions:111": {
                "pid": 111,
                "profile": "actions",
                "session_path": session_path,
                "fingerprint": "started-friday",
                "updated_at": int(time.time()),
            }
        },
    )
    _live(monkeypatch)

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
        exclusive=True,
    )

    assert issue is not None
    claims = json.loads(registry_file.read_text(encoding="utf-8"))["claims"]
    assert "actions:222" not in claims
    assert "actions:111" in claims


def test_exclusive_claim_registers_when_session_is_free(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _live(monkeypatch)

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
        exclusive=True,
    )

    assert issue is None
    assert "actions:222" in json.loads(registry_file.read_text(encoding="utf-8"))["claims"]


def test_release_session_claim_frees_the_session(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _live(monkeypatch)
    common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
        exclusive=True,
    )

    common._release_session_claim(registry_file, "actions:222")

    assert "actions:222" not in json.loads(registry_file.read_text(encoding="utf-8"))["claims"]

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:333",
        exclusive=True,
    )
    assert issue is None


def test_actions_server_starts_even_when_session_is_taken(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _write_registry(
        registry_file,
        {
            "actions:111": {
                "pid": 111,
                "profile": "actions",
                "session_path": session_path,
                "fingerprint": "started-friday",
                "updated_at": 1,
            }
        },
    )
    _live(monkeypatch)
    monkeypatch.setenv("TG_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_SESSION_CONFLICT_REGISTRY_FILE", str(registry_file))
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "fail")
    monkeypatch.delenv("TG_READ_SESSION_PATH", raising=False)
    monkeypatch.delenv("TG_ACTIONS_SESSION_PATH", raising=False)

    ctx = common.MCPServerContext(allow_session_switch=False, server_profile="actions")

    assert ctx.server_profile == "actions"


@pytest.mark.asyncio
async def test_acquire_session_refuses_and_names_owner_when_taken(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _write_registry(
        registry_file,
        {
            "actions:111": {
                "pid": 111,
                "profile": "actions",
                "session_path": session_path,
                "fingerprint": "started-friday",
                "updated_at": int(time.time()),
            }
        },
    )
    _live(monkeypatch)
    monkeypatch.setenv("TG_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_SESSION_CONFLICT_REGISTRY_FILE", str(registry_file))
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "fail")
    monkeypatch.delenv("TG_READ_SESSION_PATH", raising=False)
    monkeypatch.delenv("TG_ACTIONS_SESSION_PATH", raising=False)

    ctx = common.MCPServerContext(allow_session_switch=False, server_profile="actions")

    with pytest.raises(RuntimeError, match="pid 111"):
        await ctx.acquire_session()


@pytest.mark.asyncio
async def test_acquire_then_release_hands_the_session_over(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _live(monkeypatch)
    monkeypatch.setenv("TG_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_SESSION_CONFLICT_REGISTRY_FILE", str(registry_file))
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "fail")
    monkeypatch.delenv("TG_READ_SESSION_PATH", raising=False)
    monkeypatch.delenv("TG_ACTIONS_SESSION_PATH", raising=False)

    ctx = common.MCPServerContext(allow_session_switch=False, server_profile="actions")
    await ctx.acquire_session()

    claims = json.loads(registry_file.read_text(encoding="utf-8"))["claims"]
    assert any(value.get("profile") == "actions" for value in claims.values())

    await ctx.release_session()

    claims = json.loads(registry_file.read_text(encoding="utf-8"))["claims"]
    assert not any(value.get("profile") == "actions" for value in claims.values())


def _registry_with_owner(registry_file, session_path, *, age_sec):
    _write_registry(
        registry_file,
        {
            "actions:111": {
                "pid": 111,
                "profile": "actions",
                "session_path": session_path,
                "fingerprint": "started-friday",
                "updated_at": int(time.time()) - age_sec,
            }
        },
    )


def test_idle_owner_is_taken_over_after_timeout(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _registry_with_owner(registry_file, session_path, age_sec=600)
    _live(monkeypatch)
    monkeypatch.setenv("TG_SESSION_IDLE_TAKEOVER_SEC", "300")

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
        exclusive=True,
    )

    assert issue is None
    assert "actions:222" in json.loads(registry_file.read_text(encoding="utf-8"))["claims"]


def test_busy_owner_is_not_taken_over(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _registry_with_owner(registry_file, session_path, age_sec=10)
    _live(monkeypatch)
    monkeypatch.setenv("TG_SESSION_IDLE_TAKEOVER_SEC", "300")

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
        exclusive=True,
    )

    assert issue is not None
    assert "pid 111" in issue["message"]


def test_idle_takeover_can_be_disabled(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _registry_with_owner(registry_file, session_path, age_sec=10_000)
    _live(monkeypatch)
    monkeypatch.setenv("TG_SESSION_IDLE_TAKEOVER_SEC", "0")

    issue = common._claim_session_path(
        registry_file,
        server_profile="actions",
        session_path=session_path,
        claim_id="actions:222",
        exclusive=True,
    )

    assert issue is not None


def _actions_ctx(monkeypatch, tmp_path, registry_file, session_path):
    monkeypatch.setenv("TG_SESSION_PATH", session_path)
    monkeypatch.setenv("TG_SESSION_CONFLICT_REGISTRY_FILE", str(registry_file))
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "fail")
    monkeypatch.delenv("TG_READ_SESSION_PATH", raising=False)
    monkeypatch.delenv("TG_ACTIONS_SESSION_PATH", raising=False)
    return common.MCPServerContext(allow_session_switch=False, server_profile="actions")


@pytest.mark.asyncio
async def test_acquire_refreshes_our_claim_so_we_are_not_taken_over(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _live(monkeypatch)
    ctx = _actions_ctx(monkeypatch, tmp_path, registry_file, session_path)

    await ctx.acquire_session()
    claim_id = ctx._session_claim_id
    stale = int(time.time()) - 10_000

    def _age(claims):
        claims[claim_id]["updated_at"] = stale

    common.update_json_dict(registry_file, _age, root_key="claims")

    await ctx.acquire_session()

    claims = json.loads(registry_file.read_text(encoding="utf-8"))["claims"]
    assert claims[claim_id]["updated_at"] > stale


@pytest.mark.asyncio
async def test_evicted_owner_drops_its_connection(monkeypatch, tmp_path):
    session_path = str((tmp_path / "actions.session").resolve())
    registry_file = tmp_path / "session_registry.json"
    _live(monkeypatch)
    ctx = _actions_ctx(monkeypatch, tmp_path, registry_file, session_path)
    await ctx.acquire_session()

    client = DummyClient(username="example_account")
    ctx._client = client
    ctx._manager = object()

    # somebody else takes the session while we were idle
    def _evict(claims):
        claims.pop(ctx._session_claim_id, None)
        claims["actions:999"] = {
            "pid": 999,
            "profile": "actions",
            "session_path": session_path,
            "fingerprint": "started-friday",
            "updated_at": int(time.time()),
        }

    common.update_json_dict(registry_file, _evict, root_key="claims")

    with pytest.raises(RuntimeError, match="pid 999"):
        await ctx.acquire_session()

    assert client.disconnected is True
    assert ctx._client is None
