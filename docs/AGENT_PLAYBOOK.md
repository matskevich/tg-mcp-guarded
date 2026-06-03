# TG-MCP Agent Playbook

This guide is for AI agents and operators who need to work with `tg-mcp` quickly and safely.

## 1. Mental Model

- `tgmcp-read`: analytics and discovery only (`tg_get_*`, `tg_get_messages_since`, `tg_list_sessions`, `tg_resolve_username`, etc.).
- `tgmcp-actions`: any Telegram write (`tg_send_message`, `tg_send_file`, `tg_delete_messages`, `tg_clear_history`, `tg_leave_dialog`, `tg_edit_message`, `tg_forward_messages`, add/remove/migrate member).
- Direct Telethon write is blocked by default outside Action MCP.
- Session bootstrap auth is allowed only in explicit mode (`TG_AUTH_BOOTSTRAP=1`).
- For new installations, prefer `read` profile first (`scripts/render_mcp_config.py --profile read`).

## 2. First 60 Seconds Checklist

1. Call `tg_get_actions_policy` on `tgmcp-actions`.
2. Verify:
- `actions_enabled=true`
- `safe_startup_block_reason=null`
- `require_allowlist=true`
- `require_confirmation_text=true`
- `require_approval_code=true`
- `idempotency_enabled=true`
3. Confirm target group is present in `allowed_targets` (or update allowlist and restart Action MCP).

If any check fails, stop write execution and report policy mismatch.

## 3. Canonical Write Flow (Single Action)

1. Preview:
- call write tool with `dry_run=true`
- capture `approval_code` and `action_hash`
2. Ask user for explicit confirmation phrase in current thread.
3. Execute same payload:
- `dry_run=false`
- `confirm=true`
- exact `confirmation_text`
- `approval_code` from step 1
4. Respect approval min-age:
- if server returns "approval_code is too fresh", wait required seconds and retry execute
5. If duplicate blocked:
- wait for window or use `force_resend=true` only if user explicitly asked to resend.

Fallback when native ActionMCP tools are missing in the current thread:
- use `python3 scripts/tg_action_bridge.py tools` to verify shell-side bridge access
- use `python3 scripts/tg_action_bridge.py write-call ...` for write tools
- the bridge still talks to `mcp_server_actions.py` over MCP/JSON-RPC and keeps allowlist/approval/confirm gates

## 4. Canonical Write Flow (Batch Mission)

Use this when user wants one approval and then autonomous processing of many groups or destructive cleanup chunks.

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

## 5. Common Errors -> Agent Action

- `Actions are disabled`: stop and ask operator to set `TG_ACTIONS_ENABLED=1`.
- `allowed groups is empty`: ask operator to set `TG_ACTIONS_ALLOWED_GROUPS`.
- `Target is not in allowlist`: ask operator to add explicit target and restart server.
- `confirmation_text must be exactly ...`: request exact phrase from user.
- `approval_code is required/expired`: re-run same call with `dry_run=true` first.
- `Duplicate action blocked`: do not retry blindly; ask before `force_resend=true`.
- `batch is already running by another worker`: wait for run lease or retry later.
- `Direct Telegram write 'SendCodeRequest' is blocked`: run session bootstrap helper (sets `TG_AUTH_BOOTSTRAP=1`) or set the flag only for auth flow.

## 6. Auth Diagnostics

- Use `tg_auth_status` to verify `authorized=true/false` and current session identity.
- `tg_auth_status` now also reports `session_path_status`; treat same-session read/actions warning as real risk, not noise.
- `tgmcp-read` defaults to `TG_SESSION_RUNTIME_MODE=copy`, so duplicate read MCP processes use per-process runtime session copies instead of contending on one sqlite file.
- Use `scripts/create_session_qr.py` if app/SMS code flow is unreliable.
- Login code usually comes via Telegram app (`SentCodeTypeApp`), not SMS.
- If `.env` is restricted, use `TG_SECRET_PROVIDER=keychain|command` for `TG_API_ID/TG_API_HASH`.

## 7. Multi-Project Usage

- Shared session is supported with `TG_SESSION_LOCK_MODE=shared`.
- Shared RPS/circuit state is supported with common `data/anti_spam` and `TG_GLOBAL_RPS_MODE=shared`.
- Recommended for production:
- separate read/actions sessions by default
- read -> `*_ro.session`
- actions -> main write session
- dedicated OS user for MCP process
- configure `TG_READ_SESSION_PATH` and `TG_ACTIONS_SESSION_PATH` in both servers so runtime can detect same-session misconfig
- run `python3 scripts/check_session_paths.py --config /path/to/.mcp.json` before enabling both servers

## 8. Never Do This

- Never bypass MCP with direct Telethon writes from shell scripts.
- Never disable allowlist/confirmation/approval/idempotency in production.
- Never run bulk writes without `dry_run` or explicit user confirmation.
