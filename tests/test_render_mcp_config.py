"""onboarding must retain the virtualenv and the same safety gates from any cwd."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_mcp_config.py"


def test_generated_profiles_keep_venv_symlink_and_explicit_environment(tmp_path):
    repo = tmp_path / "repo with spaces"
    binary = repo / "venv/bin/python3"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--profile",
            "full",
            "--expected-username",
            "example_account",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    servers = json.loads(result.stdout)["mcpServers"]
    for server in servers.values():
        assert server["command"] == str(binary)
        env = server["env"]
        assert env["TG_ENV_FILE"] == str(repo / ".env")
        assert env["TG_EXPECTED_USERNAME"] == "example_account"
        assert env["TG_ENFORCE_ACTION_PROCESS"] == "1"
        assert env["TG_BLOCK_DIRECT_TELETHON_WRITE"] == "1"
        assert env["TG_ALLOW_DIRECT_TELETHON_WRITE"] == "0"
        assert all(Path(path).is_absolute() for path in server["args"])
    read = servers["tgmcp-read"]["env"]
    actions = servers["tgmcp-actions"]["env"]
    assert read["TG_SESSION_PATH"] != actions["TG_SESSION_PATH"]
    assert read["TG_ACTION_PROCESS"] == "0"
    assert actions["TG_ACTION_PROCESS"] == "1"
    assert actions["TG_ACTIONS_ALLOWED_GROUPS"] == ""
    assert actions["TG_ACTIONS_REQUIRE_APPROVAL_CODE"] == "1"
    assert actions["TG_ACTIONS_LANE_FILE"] == str(
        repo / "data/anti_spam/action_lanes.json"
    )


def test_default_profile_exposes_only_reads(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert set(json.loads(result.stdout)["mcpServers"]) == {"tgmcp-read"}
