import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_session_paths.py"


def test_preflight_script_rejects_same_session_paths(tmp_path):
    session_path = str((tmp_path / "example_account.session").resolve())
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--read-session-path",
            session_path,
            "--actions-session-path",
            session_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "share one Telegram session file" in result.stderr


def test_preflight_script_accepts_separate_paths_from_config(tmp_path):
    config_path = tmp_path / ".mcp.json"
    payload = {
        "mcpServers": {
            "tgmcp-read": {
                "env": {"TG_SESSION_PATH": str((tmp_path / "example_account_ro.session").resolve())}
            },
            "tgmcp-actions": {
                "env": {"TG_SESSION_PATH": str((tmp_path / "example_account.session").resolve())}
            },
        }
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "use separate session files" in result.stdout
