# Incoming events without Telegram history polling

The optional read-side receiver maintains a Telegram MTProto update connection.
It writes incoming new/edited messages from an explicit set of numeric peer IDs to
an account-bound local SQLite spool. It never starts a model or sends a message.
Normal `tgmcp-read` and `tgmcp-actions` keep `TG_RECEIVE_UPDATES=0`.

## Separate authorization, not a copied session

Authenticate a **new dedicated session** with the existing login helpers. Do not
copy a read/actions `.session` file: a copy separates SQLite files but retains the
same Telegram authorization. Telegram sends updates to the last active connection;
sharing authorization can steal updates from the receiver. Session paths and authorization-key equality are
checked against configured `TG_READ_SESSION_PATH`, `TG_ACTIONS_SESSION_PATH` and
`TG_SESSION_PATH`. `--independent-session` is the operator's explicit assertion of
independent login; a different filename alone does not prove that assertion. Known
configured session keys are compared in memory, never logged. Undeclared sessions
cannot be checked, so operators must supply all existing session paths.

The daemon forces the read write-guard and an exclusive session lock. It verifies
the expected numeric account ID after connecting and will not perform interactive
login. Use the normal API credential provider; do not put credentials in arguments,
logs, sample unit files or this repository.

```sh
# Replace the sample numeric IDs and paths with the operator's own verified values.
PYTHONPATH=/path/to/tg-mcp/tganalytics:/path/to/tg-mcp \
  /path/to/tg-mcp/venv/bin/python /path/to/tg-mcp/scripts/telegram_event_receiver.py \
  --session /private/sessions/events.session --independent-session \
  --account-id 100001 --peer 200002 \
  --state-dir /private/tg-events
```

Use a private owner-only state directory. Multiple `--peer` arguments are allowed.
Peer IDs use Telegram's marked ID convention (users positive, groups/channels
negative). The receiver exposes `/private/tg-events/events.sock` with mode 0600;
the parent directory has mode 0700. A single receiver holds `receiver.lock`.
For unattended execution, use a native process supervisor with restart-on-failure.
The command is opt-in; installing MCP does not start it or grant outbound authority.

## Consumption and acknowledgement

Set `TG_EVENT_SOCKET=/private/tg-events/events.sock` on the read MCP process.
`tg_get_events(after_seq=0, limit=20, timeout_seconds=0)` reads retained events;
`timeout_seconds` up to 50 waits on an in-process condition notified by new events.
There is no periodic Telegram history query during this wait. The reply includes
`receiver_ready`, `stream_id`, `events`, `next_cursor`, `oldest_seq` and `latest_seq`.

A deterministic worker can call `read_event_socket` directly, filter for the
operator's chosen trigger, then invoke a model only when needed. Do not implement
an agent repeatedly calling the wait tool after every empty result: that would
consume model turns despite zero Telegram polling. Model selection, trigger words,
reply policy and recipients belong to the consuming application, not this package.

Persist `(stream_id, next_cursor)` **after** handling the events. A changed stream ID
means a replaced database, not continuation; reconcile instead of applying an old cursor. There is no server-side
acknowledgement that consumes a shared queue. Each consumer owns its checkpoint;
re-reading a cursor replays the same retained records. Make external effects
idempotent, keyed by `(stream identity, seq)`. Actions still go through ActionMCP.
Do not infer owner authorization from a message or from receiving an event.

## Recovery and limitations

- Startup populates the dedicated session entity cache from at most 200 recent
  dialogs; only configured peers enter the spool. If an older peer cannot be
  resolved from the dedicated session, startup fails rather than widening scope.
- First launch seeds each peer's high-water mark without replaying its old history.
- Restart reconciles up to 1,000 new messages per peer, through the existing rate
  limiter, before marking the socket ready. Larger gaps stop explicitly instead
  of silently skipping them. Catch-up is a startup/recovery operation, not a timer.
- Telethon reconnects its transport and processes update differences. New-message
  recovery also uses the spool's independent high-water mark after a process restart.
- Content/version deduplication survives restart within retained events. There is no claim of exactly-once
  external actions or atomicity with Telegram's own update cursor.
- Edited-message events are captured live. Historical edits and deletions are not
  guaranteed recovered by new-message reconciliation; deletion events are not
  implemented in this initial version. Do not use this as a complete archive.
- At most 10,000 events are retained. A consumer older than retention receives
  `cursor_expired`; it must reconcile deliberately before advancing. An unconfigured
  or stopped receiver returns an explicit error, not an empty success.
- Events contain message text up to 4,096 characters and media metadata, not file
  contents. The private spool is sensitive plaintext; protect/expire it under the
  operator's data policy. No content or user-specific identifiers enter the package.
- A bounded in-memory queue protects startup. Overflow stops the process for
  supervised recovery. Socket requests and concurrent waiters are bounded.
- Receiver restart or a lost socket may interrupt a wait. Retry the same consumer
  cursor; never guess that a timeout means an event was handled.

Official protocol: [updates](https://core.telegram.org/api/updates).
Business Bot webhooks are a separate future adapter: both can feed the same
application event contract, but this implementation does not create a bot,
connect a Telegram Business account, or configure a webhook.
