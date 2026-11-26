"""Reversible forum changes retain exact approval and uncertain-effect fences."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import mcp_server_actions as actions
from tganalytics.infra import tele_client


@pytest.fixture
def topic(monkeypatch, tmp_path):
    for key, value in dict(SAFE_STARTUP_BLOCK_REASON=None, ACTIONS_ENABLED=True,
        REQUIRE_ALLOWLIST=True, ALLOWED_TARGETS={"-100123"}, REQUIRE_CONFIRMATION_TEXT=True,
        CONFIRMATION_PHRASE="confirm-test", REQUIRE_APPROVAL_CODE=True, APPROVAL_MIN_AGE_SEC=0,
        IDEMPOTENCY_ENABLED=True, APPROVAL_FILE=tmp_path/"a.json",
        IDEMPOTENCY_FILE=tmp_path/"i.json").items():
        monkeypatch.setattr(actions, key, value)
    current = SimpleNamespace(id=7, title="operations")
    entity = SimpleNamespace(forum=True, creator=True)
    manager = SimpleNamespace(_resolve_target_entity=AsyncMock(return_value=entity))
    monkeypatch.setattr(actions, "ctx", SimpleNamespace(
        get_manager=AsyncMock(return_value=manager), client=object()))
    async def call(client, request, **kw):
        if request.__class__.__name__ == "GetForumTopicsByIDRequest":
            return SimpleNamespace(topics=[current])
        return True
    safe = AsyncMock(side_effect=call)
    monkeypatch.setattr(actions, "safe_call", safe)
    return current, safe


@pytest.mark.asyncio
async def test_rename_preview_binds_exact_changes_and_source(topic):
    current, safe = topic
    args = dict(group="-100123", topic_id=7, expected_title="operations", title="сейчас", pinned=True)
    preview = await actions.tg_edit_forum_topic(**args)
    assert preview["dry_run"] and safe.await_count == 1
    bad = await actions.tg_edit_forum_topic(**{**args, "closed": True}, dry_run=False,
        confirm=True, confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert not bad["success"] and safe.await_count == 1
    current.title = "changed"
    stale = await actions.tg_edit_forum_topic(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert not stale["success"]


@pytest.mark.asyncio
async def test_success_and_no_unguarded_hidden_normal_topic(topic):
    _, safe = topic
    args = dict(group="-100123", topic_id=7, expected_title="operations", closed=True)
    preview = await actions.tg_edit_forum_topic(**args)
    result = await actions.tg_edit_forum_topic(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert result["success"] and result["independent_readback_required"]
    mutations = [c.args[1] for c in safe.await_args_list if c.args[1].__class__.__name__ == "EditForumTopicRequest"]
    assert len(mutations) == 1 and mutations[0].closed
    assert not (await actions.tg_edit_forum_topic(**args, hidden=True))["success"]
    assert not (await actions.tg_edit_forum_topic(**{**args, "group": "-100999"}))["success"]


@pytest.mark.asyncio
async def test_partial_effect_fences_retry(topic):
    _, safe = topic
    args = dict(group="-100123", topic_id=7, expected_title="operations", pinned=True)
    preview = await actions.tg_edit_forum_topic(**args)
    original = safe.side_effect
    async def lost(client, request, **kw):
        if request.__class__.__name__ == "UpdatePinnedForumTopicRequest":
            raise TimeoutError("response lost")
        return await original(client, request, **kw)
    safe.side_effect = lost
    result = await actions.tg_edit_forum_topic(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert result["reconciliation_required"]
    again = await actions.tg_edit_forum_topic(**args)
    result = await actions.tg_edit_forum_topic(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=again["approval_code"])
    assert result["duplicate_blocked"]


def test_mutations_have_process_guard():
    from tganalytics.infra.forum_schema import EditForumTopicRequest, UpdatePinnedForumTopicRequest
    for request in [EditForumTopicRequest(channel=None, topic_id=1, hidden=True),
                    UpdatePinnedForumTopicRequest(channel=None, topic_id=7, pinned=True)]:
        assert tele_client._is_telethon_write_request(request)


@pytest.mark.asyncio
async def test_title_and_close_use_separate_nonretrying_mutations(topic):
    _, safe = topic
    args = dict(group="-100123", topic_id=7, expected_title="operations",
                title="архив", closed=True, pinned=False)
    preview = await actions.tg_edit_forum_topic(**args)
    result = await actions.tg_edit_forum_topic(**args, dry_run=False, confirm=True,
        confirmation_text="confirm-test", approval_code=preview["approval_code"])
    assert result["success"]
    mutations = [c for c in safe.await_args_list if c.args[1].__class__.__name__ != "GetForumTopicsByIDRequest"]
    assert len(mutations) == 3
    assert mutations[0].args[1].title == "архив" and mutations[0].args[1].closed is None
    assert mutations[1].args[1].closed is True and mutations[1].args[1].title is None
    assert all(c.kwargs["max_retries"] == 0 for c in mutations)
