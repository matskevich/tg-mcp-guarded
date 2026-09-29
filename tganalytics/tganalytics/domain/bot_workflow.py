"""pure button serialization, selection and bot-step validation shared by mcp profiles."""

from __future__ import annotations

import base64
from typing import Any


def button_data_bytes(button: Any) -> bytes | None:
    raw_data = getattr(button, "data", None)
    if isinstance(raw_data, bytes):
        return raw_data
    if raw_data is None:
        return None
    try:
        return bytes(raw_data)
    except Exception:
        return None


def button_record(button: Any, row: int, col: int) -> dict[str, Any]:
    data = button_data_bytes(button)
    return {
        "row": row,
        "col": col,
        "text": str(getattr(button, "text", "") or ""),
        "button_type": type(getattr(button, "button", button)).__name__,
        "is_callback": data is not None,
        "data_b64": (
            base64.b64encode(data).decode("ascii") if data is not None else None
        ),
        "data_hex": data.hex() if data is not None else None,
        "url": getattr(button, "url", None),
    }


def message_button_options(msg: Any) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for row_index, row in enumerate(getattr(msg, "buttons", None) or []):
        for col_index, button in enumerate(row or []):
            record = button_record(button, row_index, col_index)
            record["_button"] = button
            options.append(record)
    return options


def public_button_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public = []
    for option in options:
        item = dict(option)
        item.pop("_button", None)
        public.append(item)
    return public


def select_button_option(
    options: list[dict[str, Any]],
    *,
    button_text: str,
    row: int,
    col: int,
    button_data_b64: str,
    exact_text: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if not options:
        return None, "message has no buttons"

    data_selector = str(button_data_b64 or "").strip()
    if data_selector:
        matches = [item for item in options if item.get("data_b64") == data_selector]
    elif row >= 0 or col >= 0:
        if row < 0 or col < 0:
            return None, "row and col must be provided together"
        matches = [
            item
            for item in options
            if int(item.get("row", -1)) == row and int(item.get("col", -1)) == col
        ]
    elif str(button_text or "").strip():
        needle = str(button_text or "").strip().lower()
        if exact_text:
            matches = [
                item
                for item in options
                if str(item.get("text") or "").strip().lower() == needle
            ]
        else:
            matches = [
                item
                for item in options
                if needle in str(item.get("text") or "").strip().lower()
            ]
    elif len(options) == 1:
        matches = list(options)
    else:
        return None, "button selector required when message has multiple buttons"

    if not matches:
        return None, "button selector did not match any button"
    if len(matches) > 1:
        return None, "button selector matched multiple buttons; use row+col or data_b64"
    return matches[0], None


def normalize_bot_steps(
    steps: Any, *, max_steps: int, max_message_len: int
) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(steps, list) or not steps:
        return [], "steps must be a non-empty list"
    if len(steps) > max_steps:
        return [], f"too many bot steps ({len(steps)} > {max_steps})"

    normalized: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            return [], f"step {index} must be an object"
        kind = str(raw_step.get("type") or raw_step.get("action") or "").strip().lower()
        if kind in {"send", "message", "text"}:
            kind = "send_message"
        if kind in {"click", "button"}:
            kind = "click_button"
        if kind not in {"send_message", "click_button", "wait"}:
            return [], f"step {index} has unsupported type: {kind!r}"

        try:
            wait_after_sec = float(raw_step.get("wait_after_sec", 1.0))
        except Exception:
            return [], f"step {index} wait_after_sec must be numeric"
        wait_after_sec = max(0.0, min(wait_after_sec, 10.0))

        if kind == "wait":
            normalized.append(
                {
                    "type": "wait",
                    "wait_after_sec": wait_after_sec,
                }
            )
            continue

        if kind == "send_message":
            text = str(raw_step.get("text") or raw_step.get("message_text") or "")
            text = text.strip()
            if not text:
                return [], f"step {index} message text is empty"
            if len(text) > max_message_len:
                return [], f"step {index} message text is too long"
            normalized.append(
                {
                    "type": "send_message",
                    "text": text,
                    "wait_after_sec": wait_after_sec,
                }
            )
            continue

        try:
            row = int(raw_step.get("row", -1))
            col = int(raw_step.get("col", -1))
            message_id = int(raw_step.get("message_id", 0) or 0)
            recent_limit = int(raw_step.get("recent_limit", 12) or 12)
        except Exception:
            return [], f"step {index} row/col/message_id/recent_limit must be integers"

        try:
            callback_timeout_sec = float(raw_step.get("callback_timeout_sec", 8.0))
        except Exception:
            return [], f"step {index} callback_timeout_sec must be numeric"

        normalized.append(
            {
                "type": "click_button",
                "message_id": max(0, message_id),
                "button_text": str(raw_step.get("button_text") or "").strip(),
                "row": row,
                "col": col,
                "button_data_b64": str(raw_step.get("button_data_b64") or "").strip(),
                "exact_text": bool(raw_step.get("exact_text", True)),
                "recent_limit": max(1, min(recent_limit, 30)),
                "callback_timeout_sec": max(1.0, min(callback_timeout_sec, 30.0)),
                "continue_on_callback_timeout": bool(
                    raw_step.get("continue_on_callback_timeout", True)
                ),
                "wait_after_sec": wait_after_sec,
            }
        )

    return normalized, None


def public_bot_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(step) for step in steps]
