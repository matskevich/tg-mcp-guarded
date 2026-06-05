import os
import sqlite3
from pathlib import Path

os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "testhash")

from tganalytics.infra import tele_client


def _dummy_request(module_name: str, class_name: str):
    cls = type(class_name, (), {})
    cls.__module__ = module_name
    return cls()


def test_write_request_detection_for_invite():
    req = _dummy_request("telethon.tl.functions.channels", "InviteToChannelRequest")
    assert tele_client._is_telethon_write_request(req) is True


def test_read_request_detection_for_get():
    req = _dummy_request("telethon.tl.functions.channels", "GetFullChannelRequest")
    assert tele_client._is_telethon_write_request(req) is False


def test_batch_write_detection():
    read_req = _dummy_request("telethon.tl.functions.messages", "GetCommonChatsRequest")
    write_req = _dummy_request("telethon.tl.functions.messages", "DeleteChatUserRequest")
    assert tele_client._contains_telethon_write_request([read_req, write_req]) is True


def test_send_code_is_blocked_without_auth_bootstrap(monkeypatch):
    req = _dummy_request("telethon.tl.functions.auth", "SendCodeRequest")
    monkeypatch.setattr(tele_client, "AUTH_BOOTSTRAP_ENABLED", False)
    assert tele_client._is_telethon_write_request(req) is True


def test_send_code_is_allowed_with_auth_bootstrap(monkeypatch):
    req = _dummy_request("telethon.tl.functions.auth", "SendCodeRequest")
    monkeypatch.setattr(tele_client, "AUTH_BOOTSTRAP_ENABLED", True)
    assert tele_client._is_telethon_write_request(req) is False


def test_load_api_credentials_from_keychain(monkeypatch):
    monkeypatch.setenv("TG_SECRET_PROVIDER", "keychain")
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    monkeypatch.setenv("TG_KEYCHAIN_SERVICE", "tg-mcp-test")
    monkeypatch.setenv("TG_KEYCHAIN_ACCOUNT_API_ID", "TG_API_ID")
    monkeypatch.setenv("TG_KEYCHAIN_ACCOUNT_API_HASH", "TG_API_HASH")

    def fake_run(cmd, capture_output, text, check):
        account = cmd[cmd.index("-a") + 1]
        value = "12345\n" if account == "TG_API_ID" else "hash_from_keychain\n"
        return type("Proc", (), {"returncode": 0, "stdout": value})()

    monkeypatch.setattr(tele_client.subprocess, "run", fake_run)
    raw_api_id, raw_api_hash = tele_client._load_api_credentials()
    assert raw_api_id == "12345"
    assert raw_api_hash == "hash_from_keychain"


def test_load_api_credentials_from_command_provider(monkeypatch):
    monkeypatch.setenv("TG_SECRET_PROVIDER", "command")
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    monkeypatch.setenv("TG_SECRET_CMD_API_ID", "secret_id_cmd")
    monkeypatch.setenv("TG_SECRET_CMD_API_HASH", "secret_hash_cmd")

    def fake_run(cmd, capture_output, text, check):
        if cmd[0] == "secret_id_cmd":
            value = "777\n"
        else:
            value = "hash_from_cmd\n"
        return type("Proc", (), {"returncode": 0, "stdout": value})()

    monkeypatch.setattr(tele_client.subprocess, "run", fake_run)
    raw_api_id, raw_api_hash = tele_client._load_api_credentials()
    assert raw_api_id == "777"
    assert raw_api_hash == "hash_from_cmd"


def test_enforce_action_process_blocks_non_action_context(monkeypatch):
    monkeypatch.setattr(tele_client, "WRITE_GUARD_ENABLED", True)
    monkeypatch.setattr(tele_client, "ALLOW_DIRECT_WRITE", False)
    monkeypatch.setattr(tele_client, "ENFORCE_ACTION_PROCESS", True)
    monkeypatch.setattr(tele_client, "WRITE_CONTEXT", "actions_mcp")
    monkeypatch.setattr(tele_client, "WRITE_ALLOWED_CONTEXTS", {"actions_mcp"})
    monkeypatch.setattr(tele_client, "_is_actions_process", lambda: False)

    assert tele_client._is_direct_write_allowed() is False


def test_enforce_action_process_allows_real_action_process(monkeypatch):
    monkeypatch.setattr(tele_client, "WRITE_GUARD_ENABLED", True)
    monkeypatch.setattr(tele_client, "ALLOW_DIRECT_WRITE", False)
    monkeypatch.setattr(tele_client, "ENFORCE_ACTION_PROCESS", True)
    monkeypatch.setattr(tele_client, "WRITE_CONTEXT", "actions_mcp")
    monkeypatch.setattr(tele_client, "WRITE_ALLOWED_CONTEXTS", {"actions_mcp"})
    monkeypatch.setattr(tele_client, "_is_actions_process", lambda: True)

    assert tele_client._is_direct_write_allowed() is True


def test_get_client_disables_updates_loop_by_default(monkeypatch, tmp_path):
    captured = {}

    class DummyClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured["session"] = session
            captured["kwargs"] = kwargs

    monkeypatch.setattr(tele_client, "_client", None)
    monkeypatch.setattr(tele_client, "_clients_by_path", {})
    monkeypatch.setattr(tele_client, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(tele_client, "session_path", str(tmp_path / "test_session"))
    monkeypatch.setattr(tele_client, "RECEIVE_UPDATES", False)
    monkeypatch.setattr(tele_client, "_acquire_session_lock", lambda *_: None)
    monkeypatch.setattr(tele_client, "_harden_session_storage", lambda *_: None)
    monkeypatch.setattr(tele_client, "GuardedTelegramClient", DummyClient)

    tele_client.get_client()

    assert captured["kwargs"]["receive_updates"] is False


def test_describe_session_target_uses_runtime_copy_for_existing_session(monkeypatch, tmp_path):
    source = tmp_path / "example_account_ro.session"
    source.write_text("seed", encoding="utf-8")

    monkeypatch.setattr(tele_client, "SESSION_RUNTIME_MODE", "copy")
    monkeypatch.setattr(tele_client, "SESSION_RUNTIME_DIR", tmp_path / "runtime")

    target = tele_client.describe_session_target(str(source))

    assert target["mode"] == "copy"
    assert target["source_session_file"] == str(source.resolve())
    assert target["effective_session_file"] != str(source.resolve())
    assert target["effective_session_file"].startswith(str((tmp_path / "runtime").resolve()))


def test_get_client_for_session_uses_runtime_copy_when_enabled(monkeypatch, tmp_path):
    source = tmp_path / "example_account_ro.session"
    with sqlite3.connect(str(source)) as conn:
        conn.execute("create table version (value text)")
        conn.execute("insert into version values ('shadow-copy-ok')")
        conn.commit()

    captured = {}

    class DummyClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured["session"] = session
            captured["kwargs"] = kwargs

    monkeypatch.setattr(tele_client, "_clients_by_path", {})
    monkeypatch.setattr(tele_client, "_runtime_session_files", set())
    monkeypatch.setattr(tele_client, "SESSION_RUNTIME_MODE", "copy")
    monkeypatch.setattr(tele_client, "SESSION_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(tele_client, "_acquire_session_lock", lambda *_: None)
    monkeypatch.setattr(tele_client, "_harden_session_storage", lambda *_: None)
    monkeypatch.setattr(tele_client, "GuardedTelegramClient", DummyClient)

    tele_client.get_client_for_session(str(source))

    runtime_session_file = Path(captured["session"]).with_suffix(".session")
    with sqlite3.connect(str(runtime_session_file)) as conn:
        value = conn.execute("select value from version").fetchone()[0]

    assert value == "shadow-copy-ok"
    assert runtime_session_file != source.resolve()
