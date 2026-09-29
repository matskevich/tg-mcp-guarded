"""State on disk must not depend on the cwd a client happened to launch us from."""

import os
from pathlib import Path

os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "testhash")

from tganalytics.infra import paths

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_project_root_points_at_repository_root():
    assert paths.PROJECT_ROOT == REPO_ROOT


def test_relative_state_path_resolves_against_project_root_not_cwd(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("TG_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    resolved = paths.resolve_state_path(
        "data/anti_spam/session_registry.json", "anti_spam", "session_registry.json"
    )

    assert resolved == REPO_ROOT / "data" / "anti_spam" / "session_registry.json"


def test_empty_state_path_falls_back_to_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv("TG_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    resolved = paths.resolve_state_path("", "anti_spam", "session_registry.json")

    assert resolved == REPO_ROOT / "data" / "anti_spam" / "session_registry.json"


def test_absolute_state_path_is_kept(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "registry.json"

    resolved = paths.resolve_state_path(
        str(target), "anti_spam", "session_registry.json"
    )

    assert resolved == target.resolve()


def test_data_root_honours_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_DATA_DIR", str(tmp_path / "state"))

    assert paths.data_root() == (tmp_path / "state").resolve()


def test_rate_limiter_counters_are_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.delenv("TG_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    from tganalytics.infra.limiter import RateLimiter

    limiter = RateLimiter()

    assert (
        limiter.counter_file == REPO_ROOT / "data" / "anti_spam" / "daily_counters.txt"
    )
    assert not (tmp_path / "data").exists()


def test_session_registry_default_is_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.delenv("TG_DATA_DIR", raising=False)
    monkeypatch.delenv("TG_SESSION_CONFLICT_REGISTRY_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    import mcp_server_common as common

    assert (
        common._session_conflict_registry_file()
        == REPO_ROOT / "data" / "anti_spam" / "session_registry.json"
    )


def test_session_registry_relative_env_is_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.delenv("TG_DATA_DIR", raising=False)
    monkeypatch.setenv(
        "TG_SESSION_CONFLICT_REGISTRY_FILE", "data/anti_spam/session_registry.json"
    )
    monkeypatch.chdir(tmp_path)

    import mcp_server_common as common

    assert (
        common._session_conflict_registry_file()
        == REPO_ROOT / "data" / "anti_spam" / "session_registry.json"
    )


def _reload(module_name):
    import importlib
    import sys

    return (
        importlib.reload(sys.modules[module_name])
        if module_name in sys.modules
        else importlib.import_module(module_name)
    )


def test_actions_state_files_are_cwd_independent(monkeypatch, tmp_path):
    for var in (
        "TG_DATA_DIR",
        "TG_ACTIONS_IDEMPOTENCY_FILE",
        "TG_ACTIONS_APPROVAL_FILE",
        "TG_ACTIONS_BATCH_FILE",
        "TG_ACTIONS_LANE_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "off")
    monkeypatch.chdir(tmp_path)

    try:
        actions = _reload("mcp_server_actions")
        anti_spam = REPO_ROOT / "data" / "anti_spam"

        assert actions.IDEMPOTENCY_FILE == anti_spam / "action_idempotency.json"
        assert actions.APPROVAL_FILE == anti_spam / "action_approvals.json"
        assert actions.BATCH_FILE == anti_spam / "action_batches.json"
        assert actions.LANE_FILE == anti_spam / "action_lanes.json"
    finally:
        monkeypatch.undo()
        _reload("mcp_server_actions")


def test_session_dir_default_is_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.delenv("TG_DATA_DIR", raising=False)
    monkeypatch.delenv("SESSION_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    try:
        tele_client = _reload("tganalytics.infra.tele_client")

        assert tele_client.SESSION_DIR == REPO_ROOT / "data" / "sessions"
        assert not (tmp_path / "data").exists()
    finally:
        monkeypatch.undo()
        _reload("tganalytics.infra.tele_client")


def test_context_sessions_dir_default_is_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.delenv("TG_DATA_DIR", raising=False)
    monkeypatch.delenv("TG_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "off")
    monkeypatch.chdir(tmp_path)

    import mcp_server_common as common

    ctx = common.MCPServerContext(allow_session_switch=False)

    assert Path(ctx.sessions_dir) == REPO_ROOT / "data" / "sessions"


def test_relative_data_root_is_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_DATA_DIR", "private-state")
    monkeypatch.chdir(tmp_path)
    assert paths.data_root() == REPO_ROOT / "private-state"


def test_lane_state_respects_data_root_and_explicit_relative_path(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("TG_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("TG_ACTIONS_LANE_FILE", raising=False)
    monkeypatch.setenv("TG_SESSION_PATH_CONFLICT_MODE", "off")
    monkeypatch.chdir(tmp_path)
    try:
        actions = _reload("mcp_server_actions")
        assert actions.LANE_FILE == tmp_path / "state/anti_spam/action_lanes.json"
        monkeypatch.setenv("TG_ACTIONS_LANE_FILE", "custom/lanes.json")
        actions = _reload("mcp_server_actions")
        assert actions.LANE_FILE == REPO_ROOT / "custom/lanes.json"
    finally:
        monkeypatch.undo()
        _reload("mcp_server_actions")
