# Action MCP Policy Toggles

This document explains which Action MCP settings can be changed, when, and how to do it safely.

If you don't need write capabilities, install only `tgmcp-read` profile and skip Action MCP entirely.

## Default-Safe Baseline

Keep these values in production:

- `TG_ACTIONS_REQUIRE_ALLOWLIST=1`
- `TG_ACTIONS_REQUIRE_CONFIRMATION_TEXT=1`
- `TG_ACTIONS_REQUIRE_APPROVAL_CODE=1`
- `TG_ACTIONS_IDEMPOTENCY_ENABLED=1`
- `TG_BLOCK_DIRECT_TELETHON_WRITE=1`
- `TG_ALLOW_DIRECT_TELETHON_WRITE=0`
- `TG_ENFORCE_ACTION_PROCESS=1`
- `TG_AUTH_BOOTSTRAP=0` (enable only temporarily for session login helpers)
- `TG_ACTIONS_UNSAFE_OVERRIDE=0`

Action MCP is fail-closed: if baseline is weakened, writes are auto-blocked unless `TG_ACTIONS_UNSAFE_OVERRIDE=1`.

## Toggle Matrix

| Variable | Safe Default | Typical Change | Safe Use Case |
|---|---|---|---|
| `TG_ACTIONS_ALLOWED_GROUPS` | explicit list | add/remove group IDs | scoped mission access |
| `TG_ACTIONS_MAX_MESSAGE_LEN` | `2000` | lower/raise | content-size policy |
| `TG_ACTIONS_MAX_FILE_MB` | `20` | lower/raise | file-size policy |
| `TG_ACTIONS_APPROVAL_TTL_SEC` | `1800` | increase slightly | slow human approval loop |
| `TG_ACTIONS_APPROVAL_MIN_AGE_SEC` | `30` | increase | force human-review pause after dry_run |
| `TG_ACTIONS_IDEMPOTENCY_WINDOW_SEC` | `86400` | shorten/extend | resend policy tuning |
| `TG_ACTIONS_BATCH_TTL_HOURS` | `168` | shorten | expire stale batches earlier |
| `TG_ACTIONS_BATCH_APPROVAL_LEASE_SEC` | `86400` | shorten/extend | one-time approval window |
| `TG_ACTIONS_BATCH_RUN_LEASE_SEC` | `1800` | increase | very long batch worker runs |
| `TG_ACTIONS_LANE_MAX_TTL_SEC` | `86400` | shorten | maximum delegated lane lifetime |
| `TG_ACTIONS_LANE_APPROVAL_TTL_SEC` | `1800` | increase slightly | slow review of a pending lane |
| `TG_ACTIONS_LANE_MAX_TARGETS` | `20` | lower | cap chats in one lane |
| `TG_ACTIONS_LANE_MAX_MESSAGES` | `50` | lower | cap total sends in one lane |
| `TG_ACTIONS_LANE_MIN_INTERVAL_SEC` | `30` | increase | reduce automatic follow-up frequency |
| `TG_ACTIONS_LANE_SEND_LOCK_SEC` | `120` | increase | unusually slow Telegram sends |
| `TG_ACTIONS_LANE_AUDIT_MAX_RECORDS` | `200` | lower/raise | local metadata audit retention |
| `TG_SESSION_LOCK_MODE` | `shared` | `exclusive` | strict one-process-per-session |
| `TG_GLOBAL_RPS_MODE` | `shared` | `local` | isolated throttling per project |

## Safe Change Procedure

1. Change env values in Action MCP config.
2. Restart Action MCP process.
3. Run `tg_get_actions_policy`.
4. Verify changed value + baseline gates still enabled.
5. Execute only `dry_run` first, then normal write flow.

## Allowlist Operations

### Expand allowlist for a mission

1. Add exact IDs/usernames to `TG_ACTIONS_ALLOWED_GROUPS`.
2. Restart Action MCP.
3. Verify new targets in `tg_get_actions_policy.allowed_targets`.
4. Run mission.
5. Remove temporary targets after mission and restart again.

### Delegate a time-bounded conversational lane

1. Keep every requested lane target in `TG_ACTIONS_ALLOWED_GROUPS`.
2. Call `tg_create_write_lane` with the smallest practical TTL, target set, quotas,
   interval and message length. Keep links disabled unless required.
3. Show the returned scope to the user and activate it once with
   `tg_approve_write_lane` plus exact confirmation text and `approval_code`.
4. Let the external monitor read through `tgmcp-read` and send only through
   `tg_send_message_with_lane`.
5. Inspect with `tg_get_write_lane(include_audit=true)` and call
   `tg_revoke_write_lane` as soon as the mission ends.

Changing a server maximum does not expand an already approved lane. A new target or
broader scope requires a new lane and a fresh human approval.

### Emergency lock-down

Set `TG_ACTIONS_ENABLED=0` and restart Action MCP.

## Values You Should Not Toggle In Production

- `TG_ACTIONS_UNSAFE_OVERRIDE=1`
- `TG_ALLOW_DIRECT_TELETHON_WRITE=1`
- `TG_ACTIONS_REQUIRE_ALLOWLIST=0`
- `TG_ACTIONS_REQUIRE_CONFIRMATION_TEXT=0`
- `TG_ACTIONS_REQUIRE_APPROVAL_CODE=0`
- `TG_ACTIONS_IDEMPOTENCY_ENABLED=0`

If you must use these for local debugging, isolate test session and revert immediately after.
