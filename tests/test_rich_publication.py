import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

import mcp_rich_publication as publication
from tganalytics.infra.rich_messages import (
    UnsupportedRichContent, decode_input, digest, discussion_root, full_message,
    prepare_rich, projection, transport,
)


@pytest.mark.asyncio
async def test_positive_numeric_self_target_is_not_converted_to_a_username():
    from tganalytics.domain.groups import GroupManager
    user = types.User(id=12345)
    client = SimpleNamespace(get_entity=AsyncMock(return_value=user))
    assert await GroupManager(client)._resolve_read_entity("12345", allow_users=True) is user
    client.get_entity.assert_awaited_once_with(12345)


@pytest.mark.asyncio
async def test_preview_only_actor_cannot_dispatch_even_with_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_RICH_PREVIEW_ONLY", "1")
    calls = []
    api = action_api(tmp_path, calls)
    result = await publication.send_rich(api, **arguments(), dry_run=False, confirm=True,
                                         confirmation_text="synthetic", approval_code="code")
    assert result["error_code"] == "rich_actor_preview_only"
    assert result["submitted"] is False
    api["ctx"].get_manager.assert_not_awaited()
    assert not (tmp_path / "rich_publication.sqlite").exists()


def content(text="synthetic\n\ntext 😀"):
    return {"_": "InputRichMessage", "blocks": [
        {"_": "PageBlockParagraph", "text": {"_": "TextPlain", "text": text}}]}


def test_layer_and_update_wrappers_do_not_hide_raw_writes_from_read_guard():
    from telethon.tl.functions import InvokeWithLayerRequest, InvokeWithoutUpdatesRequest
    from telethon.tl.functions.messages import SendMessageRequest, GetMessagesRequest
    from tganalytics.infra.tele_client import _contains_telethon_write_request
    write = SendMessageRequest(peer=types.InputPeerSelf(), message="synthetic", random_id=1)
    wrapped = InvokeWithLayerRequest(233, InvokeWithoutUpdatesRequest(write))
    assert _contains_telethon_write_request(wrapped)
    read = InvokeWithLayerRequest(233, InvokeWithoutUpdatesRequest(GetMessagesRequest(id=[])))
    assert not _contains_telethon_write_request(read)


def test_long_unicode_broken_lines_and_binary_refs_survive_transport():
    text = ("line 😀\n\nbroken\nline\n" * 1000)
    rich, preview = prepare_rich(content(text))
    assert preview["text"] == text
    assert len(text) > 12095
    assert rich.blocks[0].text.text == text
    photo = types.InputPhoto(id=4, access_hash=5, file_reference=b"\x00\xff")
    raw = transport(photo)
    json.dumps(raw)
    assert decode_input(raw).file_reference == photo.file_reference


@pytest.mark.parametrize("kind", ["SendMessageRequest", "GetUsersRequest", "InputRichMessageHTML", "__class__"])
def test_no_rpc_or_implicit_markup_constructor_in_input(kind):
    with pytest.raises(UnsupportedRichContent):
        prepare_rich({"_": kind})


def test_unknown_projection_retains_raw_without_empty_text_success():
    unknown = {"blocks": [{"_": "PageBlockFutureFeature", "secret": "synthetic"}]}
    result = projection(unknown)
    assert result["projection_complete"] is False
    assert result["text"] is None
    assert "unsupported" in result["unsupported_reason"]


def test_formatting_preserved_in_payload_and_preview_only_flattens_text():
    raw = content()
    raw["blocks"][0]["text"] = {"_": "TextBold", "text": raw["blocks"][0]["text"]}
    rich, preview = prepare_rich(raw)
    assert isinstance(rich.blocks[0].text, types.TextBold)
    assert preview["rich_message"] == raw
    assert preview["text"] == "synthetic\n\ntext 😀"
    assert digest(raw) != digest(content())


@pytest.mark.asyncio
async def test_partial_rich_requires_full_fetch_and_does_not_claim_incomplete_content(monkeypatch):
    rich = types.RichMessage(blocks=[types.PageBlockParagraph(types.TextPlain("first"))],
                             photos=[], documents=[], part=True)
    message = types.Message(id=7, peer_id=types.PeerUser(20), date=datetime.now(timezone.utc),
                            message="", rich_message=rich)
    client = SimpleNamespace(get_messages=AsyncMock(return_value=message),
                             get_input_entity=AsyncMock(return_value=types.InputPeerUser(20, 0)))
    client_call = AsyncMock(return_value=SimpleNamespace(messages=[message]))
    class Client:
        get_messages = client.get_messages
        get_input_entity = client.get_input_entity
        __call__ = client_call
    manager = SimpleNamespace(client=Client(), _resolve_read_entity=AsyncMock(return_value=types.PeerUser(20)))
    result = await full_message(manager, "20", 7)
    assert result["error_code"] == "rich_content_incomplete"
    assert client_call.await_count == 1


@pytest.mark.asyncio
async def test_discussion_matches_forward_identity_not_vector_position_or_equal_message_id():
    wrong = SimpleNamespace(id=463, fwd_from=SimpleNamespace(from_id=types.PeerChannel(8), channel_post=463))
    root = types.Message(id=999, peer_id=types.PeerChannel(22), date=datetime.now(timezone.utc),
                         message="", fwd_from=types.MessageFwdHeader(date=datetime.now(timezone.utc),
                         from_id=types.PeerChannel(11), channel_post=463))
    class Client:
        get_input_entity = AsyncMock(return_value=types.InputPeerChannel(11, 0))
        __call__ = AsyncMock(return_value=SimpleNamespace(messages=[wrong, root]))
    manager = SimpleNamespace(client=Client(), _resolve_read_entity=AsyncMock(return_value=types.PeerChannel(11)))
    result = await discussion_root(manager, "channel", 463)
    assert result["discussion_root_id"] == 999
    assert result["discussion_id"] == -1000000000022


def action_api(tmp_path, calls, allow=True):
    class Client:
        get_me = AsyncMock(return_value=SimpleNamespace(id=10))
        get_input_entity = AsyncMock(return_value=types.InputPeerUser(10, 0))
        async def get_messages(self, entity, *, ids=None, limit=None):
            if limit:
                return [SimpleNamespace(id=8)]
            return SimpleNamespace(id=ids, peer_id=types.PeerUser(10))
        async def __call__(self, request):
            calls.append(request)
            raise TimeoutError("synthetic unknown dispatch")
    manager = SimpleNamespace(client=Client(),
                              _resolve_read_entity=AsyncMock(return_value=types.PeerUser(10)))
    approvals = {}
    def gate(action_hash, dry_run, approval_code):
        if dry_run:
            approvals["code"] = action_hash
            return True, None, {"approval_code": "code"}
        return (approvals.get(approval_code) == action_hash, "approval mismatch", None)
    return {"_check_action_preconditions": lambda *a, **k: (allow, "blocked"),
            "_blocked": lambda error: {"success": False, "error": error},
            "_hash_payload": digest, "_approval_gate": gate,
            "ctx": SimpleNamespace(get_manager=AsyncMock(return_value=manager)),
            "IDEMPOTENCY_FILE": tmp_path / "idempotency.json",
            "_mark_action_executed": lambda _: None, "CONFIRMATION_PHRASE": "synthetic"}


def arguments():
    return dict(group="10", rich_message=content(), operation_id="synthetic-op-1",
                expected_actor_id=10, expected_sender_id=10, send_as_id=10)


@pytest.mark.asyncio
async def test_self_send_omits_invalid_send_as_but_keeps_approved_identity(tmp_path, monkeypatch):
    async def once(func, *args, **kwargs):
        return await func(*args)
    monkeypatch.setattr(publication, "safe_call", once)
    calls = []
    api = action_api(tmp_path, calls)
    preview = await publication.send_rich(api, **arguments())
    assert preview["actor_id"] == preview["send_as_id"] == 10
    await publication.send_rich(api, **arguments(), dry_run=False, confirm=True,
                                approval_code=preview["approval_code"])
    assert len(calls) == 1
    assert calls[0].send_as is None


@pytest.mark.asyncio
async def test_definite_sender_rejection_needs_new_approval_and_unknown_stays_fenced(tmp_path, monkeypatch):
    from telethon.errors import SendAsPeerInvalidError
    count = 0
    async def rejected(*args, **kwargs):
        nonlocal count
        count += 1
        raise SendAsPeerInvalidError(request=None)
    monkeypatch.setattr(publication, "safe_call", rejected)
    api = action_api(tmp_path, [])
    preview = await publication.send_rich(api, **arguments())
    out = await publication.send_rich(api, **arguments(), dry_run=False, confirm=True,
                                     approval_code=preview["approval_code"])
    assert out["state"] == "rejected" and out["effect_status"] == "not_performed"
    assert out["automatic_retry_allowed"] is False
    # A new process has no old approval. Rejection alone never authorizes retry.
    other = action_api(tmp_path, [])
    denied = await publication.send_rich(other, **arguments(), dry_run=False, confirm=True,
                                        approval_code="stale")
    assert denied["success"] is False and count == 1
    fresh = await publication.send_rich(other, **arguments())
    assert fresh["dry_run"] is True
    out = await publication.send_rich(other, **arguments(), dry_run=False, confirm=True,
                                     approval_code=fresh["approval_code"])
    assert count == 2 and out["state"] == "rejected"


def test_historical_rejection_migration_cannot_release_unknown_or_changed_operation(tmp_path):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / "scripts/reconcile_rich_rejection.py"
    spec = importlib.util.spec_from_file_location("rejection_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = action_api(tmp_path, [])
    with publication.ledger(api) as db:
        db.execute("INSERT INTO operations(id,hash,payload,state) VALUES ('op','hash','{}','reserved')")
    receipt = tmp_path / "receipt.json"
    base = {"action_hash": "hash", "submitted": True, "state": "unknown"}
    for error, action_hash in [("TimeoutError", "hash"), ("SendAsPeerInvalidError", "wrong")]:
        receipt.write_text(json.dumps({**base, "error_type": error, "action_hash": action_hash}))
        with pytest.raises(ValueError):
            module.reconcile(tmp_path / "rich_publication.sqlite", receipt, "op")
    receipt.write_text(json.dumps({**base, "error_type": "SendAsPeerInvalidError"}))
    assert module.reconcile(tmp_path / "rich_publication.sqlite", receipt, "op")["state"] == "rejected"
    assert module.reconcile(tmp_path / "rich_publication.sqlite", receipt, "op")["telegram_invoked"] is False


@pytest.mark.asyncio
async def test_guards_precede_identity_and_network(tmp_path):
    calls = []
    api = action_api(tmp_path, calls, allow=False)
    result = await publication.send_rich(api, **arguments())
    assert result["success"] is False
    api["ctx"].get_manager.assert_not_awaited()
    assert calls == []


@pytest.mark.asyncio
async def test_payload_changes_invalidate_approval_before_send(tmp_path):
    calls = []
    api = action_api(tmp_path, calls)
    preview = await publication.send_rich(api, **arguments())
    args = arguments()
    args["reply_to_msg_id"] = 9
    result = await publication.send_rich(api, **args, dry_run=False, confirm=True,
                                         approval_code=preview["approval_code"])
    assert result["success"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_dispatch_stays_reserved_across_new_process_ledger_and_no_force_retry(tmp_path, monkeypatch):
    async def once(func, *args, **kwargs):
        assert kwargs["max_retries"] == 0
        return await func(*args)
    monkeypatch.setattr(publication, "safe_call", once)
    calls = []
    api = action_api(tmp_path, calls)
    preview = await publication.send_rich(api, **arguments())
    result = await publication.send_rich(api, **arguments(), dry_run=False, confirm=True,
                                         approval_code=preview["approval_code"])
    assert result["state"] == "unknown"
    assert len(calls) == 1
    # A new actor process would have new approval/duplicate caches. The durable
    # operation reservation must still fence the write.
    other = action_api(tmp_path, calls)
    again = await publication.send_rich(other, **arguments(), dry_run=False, confirm=True,
                                        approval_code="new-code")
    assert again["reconciliation_required"] is True
    assert again["new_dispatch"] is False
    assert len(calls) == 1


def test_server_auto_url_normalization_preserves_text_but_not_changed_style_or_explicit_link():
    from tganalytics.infra.rich_messages import canonical_rich_tree
    plain = {"_":"TextPlain", "text":"a https://example.test 😀\n"}
    normalized = {"_":"TextConcat", "texts":[{"_":"TextPlain","text":"a "},
        {"_":"TextAutoUrl","text":{"_":"TextPlain","text":"https://example.test"}},
        {"_":"TextPlain","text":" 😀\n"}]}
    assert canonical_rich_tree(normalized) == plain
    assert projection({"blocks":[{"_":"PageBlockParagraph","text":normalized}]})["text"] == plain["text"]
    assert canonical_rich_tree({"_":"TextBold","text":plain}) != plain
    assert canonical_rich_tree({"_":"TextUrl","text":plain,"url":"https://other.test"}) != plain
    assert canonical_rich_tree({"_":"TextAutoUrl","text":plain,"future_field":True}) != plain


@pytest.mark.asyncio
async def test_full_read_uses_exact_thread_when_ids_lookup_cannot_find_rich_reply():
    rich = types.RichMessage(blocks=[types.PageBlockParagraph(types.TextPlain("complete reply"))],
                             photos=[], documents=[])
    reply = types.Message(id=99, peer_id=types.PeerChannel(20), date=datetime.now(timezone.utc),
        message="", rich_message=rich, from_id=types.PeerChannel(11),
        reply_to=types.MessageReplyHeader(reply_to_msg_id=98))
    class Client:
        get_messages = AsyncMock(return_value=None)
        get_input_entity = AsyncMock(return_value=types.InputPeerChannel(20,0))
        __call__ = AsyncMock(return_value=SimpleNamespace(messages=[reply]))
    manager = SimpleNamespace(client=Client(),
        _resolve_read_entity=AsyncMock(return_value=types.PeerChannel(20)))
    result = await full_message(manager, "-1000000000020", 99, thread_root_id=98)
    assert result["content_complete"] is True and result["text"] == "complete reply"
    assert result["retrieval"]["method"] == "messages.getReplies"
    request = manager.client.__call__.call_args.args[0]
    assert request.msg_id == 98 and request.min_id == 98 and request.max_id == 100
    reply.reply_to.reply_to_msg_id = 97
    assert (await full_message(manager,"-1000000000020",99,thread_root_id=98))["error_code"] == "message_thread_mismatch"
    reply.reply_to.reply_to_msg_id = 98
    reply.peer_id = types.PeerChannel(21)
    assert (await full_message(manager,"-1000000000020",99,thread_root_id=98))["error_code"] == "message_peer_mismatch"
