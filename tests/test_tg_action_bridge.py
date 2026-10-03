import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tg_action_bridge.py"


FAKE_SERVER = r"""
import json
import os
import sys
import time

preview_time = None

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    req = json.loads(raw)
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-actions", "version": "0.1"},
            },
        }
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": "tg_echo_env"},
                    {"name": "tg_send_message"},
                ]
            },
        }
    elif method == "tools/call":
        name = req["params"]["name"]
        args = req["params"]["arguments"]
        if name == "tg_echo_env":
            body = {
                "runtime_mode": os.environ.get("TG_SESSION_RUNTIME_MODE"),
                "runtime_profile": os.environ.get("TG_SESSION_RUNTIME_PROFILE"),
                "custom": os.environ.get("BRIDGE_TEST_FLAG"),
            }
        elif name == "tg_send_message":
            if args.get("dry_run") is True:
                preview_time = time.monotonic()
                body = {
                    "success": True,
                    "approval_code": "abc123",
                    "message_text": args.get("message_text"),
                }
                if os.environ.get("BRIDGE_TEST_PREVIEW_POLICY") == "1":
                    body["confirmation_text_required"] = "confirm-from-preview"
                    body["approval_min_age_sec"] = 0.05
            else:
                body = {
                    "success": os.environ.get("BRIDGE_TEST_EXECUTION_FAIL") != "1",
                    "confirm": args.get("confirm"),
                    "approval_code": args.get("approval_code"),
                    "confirmation_text": args.get("confirmation_text"),
                    "dry_run": args.get("dry_run"),
                    "message_text": args.get("message_text"),
                    "elapsed_since_preview": time.monotonic() - preview_time,
                }
        else:
            body = {"error": f"unknown tool {name}"}
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]
            },
        }
    else:
        resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": method}}

    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()
"""


def _write_fake_server(tmp_path: Path) -> Path:
    server = tmp_path / "fake_actions_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    return server


def _write_config(tmp_path: Path, server_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[mcp_servers.tgmcp_actions]",
                f'command = "{sys.executable}"',
                f'args = ["{server_path}"]',
                "",
                "[mcp_servers.tgmcp_actions.env]",
                'TG_ACTIONS_CONFIRMATION_PHRASE = "confirm-test-action"',
                'TG_ACTIONS_APPROVAL_MIN_AGE_SEC = "0"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config


def test_bridge_call_supports_server_alias_and_runtime_copy(tmp_path):
    server = _write_fake_server(tmp_path)
    config = _write_config(tmp_path, server)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--server",
            "tgmcp-actions",
            "--set-env",
            "BRIDGE_TEST_FLAG=ok",
            "call",
            "tg_echo_env",
            "--args-json",
            "{}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runtime_mode"] == "copy"
    assert payload["runtime_profile"] == "actions_bridge"
    assert payload["custom"] == "ok"


def test_bridge_write_call_runs_preview_then_confirm(tmp_path):
    server = _write_fake_server(tmp_path)
    config = _write_config(tmp_path, server)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "write-call",
            "tg_send_message",
            "--args-json",
            '{"group":"@bot","message_text":"hello"}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["preview"]["approval_code"] == "abc123"
    assert payload["execution"]["confirm"] is True
    assert payload["execution"]["approval_code"] == "abc123"
    assert payload["execution"]["confirmation_text"] == "confirm-test-action"
    assert payload["execution"]["dry_run"] is False
    assert payload["execution"]["message_text"] == "hello"


def test_bridge_uses_preview_policy_when_runtime_env_is_hidden(tmp_path):
    server = _write_fake_server(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[mcp_servers.tgmcp_actions]\ncommand = "{sys.executable}"\n'
        f'args = ["{server}"]\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--set-env",
            "BRIDGE_TEST_PREVIEW_POLICY=1",
            "write-call",
            "tg_send_message",
            "--args-json",
            '{"group":"@self","message_text":"hello"}',
            "--approval-wait-sec",
            "0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["execution"]["confirmation_text"] == "confirm-from-preview"
    assert payload["execution"]["elapsed_since_preview"] >= 0.05


def test_bridge_returns_failure_when_execution_is_blocked(tmp_path):
    server = _write_fake_server(tmp_path)
    config = _write_config(tmp_path, server)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--set-env",
            "BRIDGE_TEST_EXECUTION_FAIL=1",
            "write-call",
            "tg_send_message",
            "--args-json",
            '{"group":"@self","message_text":"hello"}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["execution"]["success"] is False
