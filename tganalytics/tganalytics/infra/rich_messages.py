"""Lossless rich-message transport and explicit rendering boundaries.

Raw TL trees remain available when a text projection is unsupported. Binary
references use tagged base64, never an implicit UTF-8 conversion.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import inspect
import json


class UnsupportedRichContent(ValueError):
    pass


def transport(value):
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: transport(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [transport(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise UnsupportedRichContent("unsupported transport type: " + type(value).__name__)


def digest(value):
    return hashlib.sha256(json.dumps(transport(value), sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def rich_text(node):
    kind = node.get("_")
    if kind == "TextEmpty":
        return ""
    if kind == "TextPlain":
        return node["text"]
    if kind == "TextConcat":
        return "".join(rich_text(item) for item in node["texts"])
    if kind in {"TextBold", "TextItalic", "TextUnderline", "TextStrike", "TextFixed",
                "TextUrl", "TextEmail", "TextPhone", "TextSubscript", "TextSuperscript",
                "TextMarked", "TextAnchor", "TextSpoiler"}:
        return rich_text(node["text"])
    raise UnsupportedRichContent("unsupported rich text: " + str(kind))


def block_text(block):
    kind = block.get("_")
    if kind in {"PageBlockParagraph", "PageBlockHeader", "PageBlockSubheader",
                "PageBlockTitle", "PageBlockSubtitle", "PageBlockFooter",
                "PageBlockPreformatted", "PageBlockHeading1", "PageBlockHeading2",
                "PageBlockHeading3", "PageBlockHeading4", "PageBlockHeading5",
                "PageBlockHeading6"}:
        return rich_text(block["text"])
    if kind in {"PageBlockDivider", "PageBlockAnchor"}:
        return ""
    # A media/list/table tree is still returned in full; do not silently invent
    # a text-only representation or claim complete rendering of an unknown kind.
    raise UnsupportedRichContent("unsupported text projection: " + str(kind))


def projection(rich):
    raw = transport(rich)
    try:
        text = "\n\n".join(block_text(block) for block in raw["blocks"])
        return {"text": text, "projection_complete": True}
    except UnsupportedRichContent as error:
        return {"text": None, "projection_complete": False,
                "unsupported_reason": str(error)}


def decode_input(value):
    """Only data constructors, never a callable/function or arbitrary class."""
    from telethon.tl import types

    if isinstance(value, list):
        return [decode_input(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"encoding", "data"} and value["encoding"] == "base64":
        return base64.b64decode(value["data"], validate=True)
    kind = value.get("_")
    allowed = (isinstance(kind, str) and
               (kind.startswith("PageBlock") or kind.startswith("Text") or
                kind in {"InputRichMessage", "InputPhoto", "InputDocument", "InputUser",
                         "PageCaption", "PageListItemText", "PageListItemBlocks",
                         "PageListOrderedItemText", "PageListOrderedItemBlocks",
                         "PageTableRow", "PageTableCell"}))
    constructor = getattr(types, kind, None) if allowed else None
    if constructor is None:
        raise UnsupportedRichContent("unsupported input constructor: " + str(kind))
    fields = {key: decode_input(item) for key, item in value.items() if key != "_"}
    try:
        inspect.signature(constructor).bind(**fields)
        return constructor(**fields)
    except (TypeError, ValueError) as error:
        raise UnsupportedRichContent("invalid constructor fields: " + kind) from error


def prepare_rich(value):
    """Exact blocks allow a local preview without server-side markup rewriting."""
    if not isinstance(value, dict) or value.get("_") != "InputRichMessage":
        raise UnsupportedRichContent("expected InputRichMessage blocks; html/markdown parsing is not silently substituted")
    raw = transport(value)
    if len(json.dumps(raw).encode()) > 1024 * 1024:
        raise UnsupportedRichContent("rich payload exceeds local 1 MiB transport bound")
    result = decode_input(raw)
    preview = projection({"blocks": raw.get("blocks", [])})
    return result, {**preview, "rich_sha256": digest(raw), "rich_message": raw}


async def full_message(manager, group, message_id):
    from telethon import utils
    from ..domain.groups import _safe_api_call

    if type(message_id) is not int or message_id <= 0:
        raise ValueError("positive message_id required")
    entity = await manager._resolve_read_entity(group, allow_users=True)
    if entity is None:
        return {"success": False, "error_code": "target_not_resolved"}
    message = await _safe_api_call(manager.client.get_messages, entity, ids=message_id)
    if message is None or type(message).__name__ == "MessageEmpty":
        return {"success": False, "error_code": "message_not_found"}
    expected_peer = utils.get_peer_id(entity)
    if utils.get_peer_id(message.peer_id) != expected_peer:
        return {"success": False, "error_code": "message_peer_mismatch"}
    rich = getattr(message, "rich_message", None)
    if rich is not None and getattr(rich, "part", False):
        from telethon.tl.functions.messages import GetRichMessageRequest
        response = await _safe_api_call(manager.client, GetRichMessageRequest(
            peer=await manager.client.get_input_entity(entity), id=message_id))
        message = next((item for item in response.messages if item.id == message_id), None)
        rich = getattr(message, "rich_message", None)
        if (rich is None or getattr(rich, "part", False) or
                utils.get_peer_id(message.peer_id) != expected_peer):
            return {"success": False, "error_code": "rich_content_incomplete"}
    raw = transport(message)
    if rich is not None:
        rendered = projection(rich)
        return {"success": True, "message_id": message_id, "format": "rich",
                "content_complete": True, **rendered, "raw_message": raw,
                "rich_sha256": digest(rich)}
    if type(getattr(message, "media", None)).__name__ == "MessageMediaUnsupported":
        return {"success": False, "message_id": message_id,
                "error_code": "unsupported_message_format", "content_complete": False,
                "raw_message": raw}
    return {"success": True, "message_id": message_id, "format": "text_or_media",
            "text": message.message or "", "content_complete": True,
            "raw_message": raw}


async def discussion_root(manager, channel, post_id):
    from telethon import utils
    from telethon.tl.functions.messages import GetDiscussionMessageRequest
    from ..domain.groups import _safe_api_call

    entity = await manager._resolve_read_entity(channel, allow_users=False)
    peer = await manager.client.get_input_entity(entity)
    source_id = utils.get_peer_id(peer)
    response = await _safe_api_call(manager.client, GetDiscussionMessageRequest(peer=peer, msg_id=post_id))
    matches = []
    for message in response.messages:
        forward = getattr(message, "fwd_from", None)
        if (forward is not None and getattr(forward, "from_id", None) is not None and
                utils.get_peer_id(forward.from_id) == source_id and
                getattr(forward, "channel_post", None) == post_id):
            matches.append(message)
    if len(matches) != 1:
        return {"success": False, "error_code": "discussion_root_not_uniquely_verified"}
    root = matches[0]
    return {"success": True, "channel_id": source_id, "channel_post_id": post_id,
            "discussion_id": utils.get_peer_id(root.peer_id), "discussion_root_id": root.id,
            "raw_root": transport(root)}
