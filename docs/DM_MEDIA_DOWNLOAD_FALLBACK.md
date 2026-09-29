# DM media download fallback

## Problem

`tg_get_messages` can return a direct-message photo with `has_media=true` while
`tg_download_media(group, message_id)` returns `Download failed or message has no
media` for the same peer and message ID.

The failure occurs when Telethon's exact `get_messages(entity, ids=message_id)`
lookup returns no message even though the message is present in the peer's
`iter_messages` history.

## Implemented behavior

`GroupManager.download_media` now:

1. resolves the requested peer, including direct-user dialogs;
2. attempts the exact ID lookup first;
3. if the lookup is empty or has no media, scans at most 500 recent messages in
   that same peer for the requested ID;
4. downloads the full `Message` object when a matching media message is found;
5. preserves the existing failure response when no matching media exists.

The fallback is read-only, peer-scoped, bounded, and remains inside the existing
`_safe_api_call` rate-limit path. It does not perform a global cross-dialog scan.

## Regression coverage

`tests/test_group_manager.py::test_download_media_falls_back_to_peer_history`
proves that an empty exact lookup falls back to peer history and passes the full
message object to Telethon's downloader.

Before opening a pull request, run:

```bash
PYTHONPATH=tganalytics:. python -m pytest tests/ -q
make anti-spam-check
make security-check
```

## Remaining validation

- add or run an opt-in integration test against a recent DM photo/album;
- verify the returned local path and file MIME type;
- restart the read MCP process so it loads the updated implementation;
- repeat `tg_get_messages` followed by `tg_download_media` for the same ID.

Do not restart the only working live session until its persisted session file
passes the configured `TG_EXPECTED_USERNAME` identity check. Session persistence
is a separate issue from the download fallback and should be fixed independently.

## Suggested pull request

Title: `fix: fall back to peer history for DM media downloads`

Scope:

- `tganalytics/tganalytics/domain/groups.py`
- `tests/test_group_manager.py`
- `docs/DM_MEDIA_DOWNLOAD_FALLBACK.md`

Risk: the fallback may inspect up to 500 recent messages after a failed exact
lookup. It never crosses the resolved peer and does not change Telegram state.
