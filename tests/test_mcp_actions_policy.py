from mcp_actions_policy import detect_unsafe_defaults


def _safe_env() -> dict[str, str]:
    return {
        "TG_BLOCK_DIRECT_TELETHON_WRITE": "1",
        "TG_ALLOW_DIRECT_TELETHON_WRITE": "0",
        "TG_ENFORCE_ACTION_PROCESS": "1",
        "TG_ACTIONS_ALLOWED_GROUPS": "-1001234567890",
        "TG_ACTIONS_CONFIRMATION_PHRASE": "confirm-test-action",
    }


def test_detect_unsafe_defaults_flags_empty_allowlist_when_required():
    env = _safe_env()
    env["TG_ACTIONS_ALLOWED_GROUPS"] = ""

    issues = detect_unsafe_defaults(
        env=env,
        require_allowlist=True,
        require_confirmation_text=True,
        require_approval_code=True,
        idempotency_enabled=True,
    )

    assert any("TG_ACTIONS_ALLOWED_GROUPS" in issue for issue in issues)


def test_detect_unsafe_defaults_allows_non_empty_allowlist():
    issues = detect_unsafe_defaults(
        env=_safe_env(),
        require_allowlist=True,
        require_confirmation_text=True,
        require_approval_code=True,
        idempotency_enabled=True,
    )

    assert issues == []
