# TG-MCP Agent Playbook

This guide is for AI agents and operators who need to work with `tg-mcp` quickly and safely.

new operator: start with [the bootstrap prompt and skill pack](AGENT_START.md). use [recipes](RECIPES.md) for concrete tool calls, delivery receipts, assistant voice and voice notes. copied skills must point to the operator's own private profile and repository.

## 1. Mental Model

- `tgmcp-read`: analytics and discovery only (`tg_get_*`, `tg_get_messages_since`, `tg_list_sessions`, `tg_resolve_username`, etc.).
- `tgmcp-actions`: any Telegram write (`tg_send_message`, `tg_send_file`, `tg_click_inline_button`, `tg_run_bot_steps`, `tg_delete_messages`, `tg_clear_history`, `tg_leave_dialog`, `tg_edit_message`, `tg_forward_messages`, add/remove/migrate member).
- Direct Telethon write is blocked by default outside Action MCP.
- Session bootstrap auth is allowed only in explicit mode (`TG_AUTH_BOOTSTRAP=1`).
- For new installations, prefer `read` profile first (`scripts/render_mcp_config.py --profile read`).

## 2. First 60 Seconds Checklist

1. discover the tools exposed by your configured read server; call `tg_auth_status` and compare the actual account with your operator profile. stop on mismatch. `TG_EXPECTED_USERNAME` is the automated guard; numeric id requires an explicit agent-side comparison in this version.
2. for a read-only installation, read five dialogs through `tg_get_my_dialogs(limit=5)` and continue with a bounded task. actions is optional.
3. when actions is connected, verify its `tg_auth_status` reports the same owner id, then call `tg_get_actions_policy` on that server. verify:
- `actions_enabled=true`
- `safe_startup_block_reason=null`
- `require_allowlist=true`
- `require_confirmation_text=true`
- `require_approval_code=true`
- `idempotency_enabled=true`
4. confirm the exact authorized target is present in `allowed_targets`. if it is missing, prepare a mission-scoped configuration change within owner authority; never disable the allowlist to complete a task.

If any check fails, stop write execution and report policy mismatch.

## 3. Canonical Write Flow (Single Action)

1. verify owner authority for the exact target and payload. a direct ordinary send request with known content is sufficient one-action authority; the backend phrase is a process guard. ask only for missing scope or a consequential choice. capture the latest message id as a read-back watermark.
2. preview with `dry_run=true`; retain `approval_code`, `action_hash` and the exact required process confirmation text.
3. wait until `approval_execute_after_ts` (or the returned minimum age). if the server still reports a fresh code, wait the remaining interval.
4. execute the same payload:
- `dry_run=false`
- `confirm=true`
- exact `confirmation_text`
- `approval_code` from step 2
5. If duplicate blocked:
- wait for window or use `force_resend=true` only if user explicitly asked to resend.
6. read back the new message in the same verified peer and compare the exact payload and sender. preserve the message id and action hash. `success=true` alone is not a delivery receipt; see the [watermark-based flow](RECIPES.md#отправить-конкретный-текст-и-получить-receipt). never infer that the recipient read, accepted or paid from a successful send.

Fallback when native ActionMCP tools are missing in the current thread:
- use `python3 scripts/tg_action_bridge.py tools` to verify shell-side bridge access
- use `python3 scripts/tg_action_bridge.py write-call ...` for write tools
- the bridge still talks to `mcp_server_actions.py` over MCP/JSON-RPC and keeps allowlist/approval/confirm gates
- this version's `auto` wait reads bridge env; explicitly pass the policy minimum age and exact configured phrase when server wrappers hide env. inspect json success fields, not only exit status. see [the fallback recipe](RECIPES.md#отправить-конкретный-текст-и-получить-receipt).

## 4. Canonical Write Flow (Batch Mission)

Use this when user wants one approval and then autonomous processing of many groups or destructive cleanup chunks.

For stateful Telegram bot setup, prefer `tg_run_bot_steps` when the flow can be expressed as an explicit sequence of `send_message` / `click_button` / `wait` steps in one allowlisted bot dialog.

1. Create one batch:
- add member: `tg_create_add_member_batch(user, groups, note)`
- delete cleanup: `tg_create_delete_messages_batch_from_manifest(manifest_path)`
- leave cleanup: `tg_create_leave_dialog_batch_from_candidates(candidates_path)`
2. `tg_approve_batch(batch_id, confirmation_text)` once
3. Loop:
- `tg_run_add_member_batch(batch_id, max_actions=...)`, `tg_run_delete_messages_batch(...)`, or `tg_run_leave_dialog_batch(...)`
- `tg_get_batch_status(batch_id)` for progress
4. If stopped by quota:
- wait for quota reset, continue with `tg_run_add_member_batch`
5. If approval lease expired:
- re-run `tg_approve_batch`, continue same batch

## 5. Time-Bounded Conversational Write Lane

Use this when a user approves autonomous follow-ups in a fixed set of chats for a
limited time. Reads and scheduling remain separate: the heartbeat checks chats through
`tgmcp-read`; the lane only authorizes constrained text sends through `tgmcp-actions`.

1. Create an inert scope with `tg_create_write_lane(name, purpose, targets, ttl_sec,
   max_messages, max_messages_per_target, min_interval_sec, max_message_len,
   allow_links=false)`.
2. verify owner authority covers the complete returned target/purpose/ttl/quota envelope. if already explicitly granted, satisfy the configured process confirmation internally. otherwise ask for the missing consequential grant; do not use the backend phrase as a substitute for scope authorization.
3. Activate once with `tg_approve_write_lane(lane_id, confirmation_text,
   approval_code)`.
4. For each proposed reply, call `tg_send_message_with_lane(..., dry_run=true)` when
   human inspection is useful, or `dry_run=false` when the active lane already covers it.
5. Check `tg_get_write_lane(include_audit=true)` after activity. Revoke immediately with
   `tg_revoke_write_lane` when the task ends or the observed conversation leaves scope.

The `purpose` field is human-readable audit context. The server structurally enforces
action type, target set, account, TTL, quotas, interval, length and link policy; it does
not infer whether prose semantically matches the purpose. The monitoring agent must
keep the approved conversational direction in its prompt.

Lane authority is text-only and cannot be widened in place. Files, forwards, edits,
deletes, member actions and new targets use the normal approval flows.

## 6. Common Errors -> Agent Action

- `Actions are disabled`: stop and ask operator to set `TG_ACTIONS_ENABLED=1`.
- `allowed groups is empty`: ask operator to set `TG_ACTIONS_ALLOWED_GROUPS`.
- `Target is not in allowlist`: ask operator to add explicit target and restart server.
- `confirmation_text must be exactly ...`: use the exact configured phrase from policy/preview when the action is already authorized; ask the owner only if authority or required configuration is actually missing.
- `approval_code is required/expired`: re-run same call with `dry_run=true` first.
- `Duplicate action blocked`: do not retry blindly; ask before `force_resend=true`.
- `batch is already running by another worker`: wait for run lease or retry later.
- `Direct Telegram write 'SendCodeRequest' is blocked`: run session bootstrap helper (sets `TG_AUTH_BOOTSTRAP=1`) or set the flag only for auth flow.

## 7. Auth Diagnostics

- Use `tg_auth_status` to verify `authorized=true/false` and current session identity.
- `tg_auth_status` now also reports `session_path_status`; treat same-session read/actions warning as real risk, not noise.
- `tgmcp-read` defaults to `TG_SESSION_RUNTIME_MODE=copy`, so duplicate read MCP processes use per-process runtime session copies instead of contending on one sqlite file.
- Use `scripts/create_session_qr.py` if app/SMS code flow is unreliable.
- Login code usually comes via Telegram app (`SentCodeTypeApp`), not SMS.
- If `.env` is restricted, use `TG_SECRET_PROVIDER=keychain|command` for `TG_API_ID/TG_API_HASH`.

## 8. Multi-Project Usage

- `TG_SESSION_LOCK_MODE=shared` controls the local file lock; it does not remove sqlite write contention. use separate read/actions session files and per-process runtime copies where configured. two session files still authorize the same full telegram account; the read boundary is enforced by mcp.
- Shared RPS/circuit state is supported with common `data/anti_spam` and `TG_GLOBAL_RPS_MODE=shared`.
- Recommended for production:
- separate read/actions sessions by default
- read -> `*_ro.session`
- actions -> main write session
- dedicated OS user for MCP process
- configure `TG_READ_SESSION_PATH` and `TG_ACTIONS_SESSION_PATH` in both servers so runtime can detect same-session misconfig
- run `python3 scripts/check_session_paths.py --config /path/to/.mcp.json` before enabling both servers

## 9. Never Do This

- Never bypass MCP with direct Telethon writes from shell scripts.
- Never disable allowlist/confirmation/approval/idempotency in production.
- Never run bulk writes without `dry_run` or explicit user confirmation.
