# Time-Bounded Write Lanes

A write lane is a human-approved, temporary capability for an agent to send short text
follow-ups in a fixed set of Telegram chats. It composes with an external heartbeat:

1. the heartbeat reads new messages through `tgmcp-read`;
2. agent logic decides whether a follow-up is needed;
3. `tgmcp-actions` validates and sends through the approved lane.

The Action MCP does not schedule checks and the read MCP never receives write authority.

## Security boundary

One approval fixes an immutable scope:

- explicit targets that must also remain in `TG_ACTIONS_ALLOWED_GROUPS`;
- TTL, capped by `TG_ACTIONS_LANE_MAX_TTL_SEC` (24 hours by default);
- total and per-target message quotas;
- minimum interval between messages to the same target;
- maximum message length;
- links disabled by default;
- optional local daily active hours, bound to an IANA timezone;
- the Telegram account ID active at approval time.

Normal global rate limiting, circuit breaking and payload idempotency still apply. Lane
state uses the locked, atomic Action MCP JSON store. Audit records keep timestamps,
target, outcome, message length and action hash, but not message text. An approval-bound
scope hash makes later target or policy changes fail closed.

Version 1 delegates only `send_message`. It excludes files, forwarding, editing,
deletion, history clearing, membership actions and target discovery.

`purpose` records the approved human intent. It is not a semantic classifier: the
server cannot prove that arbitrary prose follows a style or investigation direction.
Put that behavioral constraint in the heartbeat/agent prompt and keep structural scope
narrow enough to limit the consequence of a bad decision.

## Canonical flow

1. Call `tg_create_write_lane`. The result is pending and cannot write.
2. Show the exact targets, purpose, TTL, quotas, interval, length and link policy to the
   user, together with the normal confirmation requirement.
3. Call `tg_approve_write_lane` with the exact `confirmation_text` and one-time
   `approval_code`. The lane binds to the live Telegram account and its TTL starts now.
4. The monitor calls `tg_send_message_with_lane`. No per-message confirmation is needed
   while every constraint still passes.
5. Inspect with `tg_get_write_lane` or `tg_list_write_lanes`; terminate with
   `tg_revoke_write_lane`.

Example 24-hour survey scope:

```json
{
  "name": "equipment field survey",
  "purpose": "ask short targeted follow-ups about photographed network equipment",
  "targets": ["@field_helper", "-1001234567890"],
  "ttl_sec": 86400,
  "max_messages": 20,
  "max_messages_per_target": 12,
  "min_interval_sec": 300,
  "max_message_len": 700,
  "allow_links": false,
  "active_hours_start": "06:00",
  "active_hours_end": "21:00",
  "active_timezone": "Asia/Makassar"
}
```

## Failure behavior

The lane fails closed when approval or lease expires, quota is exhausted, a target
leaves the static allowlist, account identity changes, content violates the length/link
policy, local time is outside the approved half-open daily window, a duplicate is
detected, or another worker owns the send lease. For example, `06:00`-`21:00` allows
sends from 06:00 through 20:59 in the specified timezone.

Revocation prevents every new send acquisition immediately. A Telegram request already
in flight cannot be cancelled reliably and may complete; finalization preserves that
outcome in the lane audit.

A scope cannot be edited or extended. Create and approve a new lane for added targets,
longer time, more quota, links, or any other wider authority.

Telegram and the local state file cannot form one atomic transaction. If a process dies
after Telegram accepts a message but before local finalization, delivery is uncertain.
Successful sends reserve global idempotency as soon as Telegram returns; monitors must
still avoid blind retry after a crash and inspect the chat before deciding.
