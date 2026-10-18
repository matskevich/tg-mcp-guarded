---
name: tg-guarded-actions
description: execute an authorized telegram send or other write through tg-mcp actions with identity checks, exact scope, approval gates and read-back verification.
---

# guarded telegram actions

load the private operator profile in your workspace: expected account username and
numeric user id, server aliases, repository path, permitted recipients and authority
rules. inspect installed tool schemas and current `tg_get_actions_policy`.

## authority and policy

human authority answers who, what and why; backend process confirmation validates
the concrete api operation. keep these separate in the receipt. an explicit ordinary
one-off send request with clear target and content supplies authority for that send;
do not ask the owner to repeat a magic phrase solely to fill an api parameter.
read the exact backend confirmation text from policy or preview, pass it as the
process parameter, and never represent that value as a new user-authored statement.
if target, payload or substantive authority is missing, resolve that missing fact.

all writes use the actions server, including files, forwards, edits, deletes, bot
buttons and membership changes. never bypass it with direct telethon, raw requests
or another session. an existing mcp bridge may transport the same guarded tool call.
retain allowlist, confirmation, approval, idempotency and `TG_ENFORCE_ACTION_PROCESS=1`.
stop for disabled actions, unsafe startup policy or account/target mismatch.

## one action

1. verify `tg_auth_status` on both routes against the operator profile. resolve the
   live username and `account.id` explicitly; the backend guard only checks username.
   resolve the stable numeric target through read tools and the actions allowlist.
2. save the latest target message id as a watermark and prepare the exact payload.
   call the intended write tool with `dry_run=true`.
3. compare returned target, payload scope and `action_hash` with human authority.
   retain the matching one-time `approval_code` and `confirmation_text_required`.
4. wait the configured approval minimum age. execute the same payload using
   `dry_run=false`, `confirm=true`, the exact process `confirmation_text` and code.
   a changed payload needs a new dry-run; an expired code needs a fresh preview.
5. read back through the read server in the same peer. for sends, require a new
   message id above the watermark, the expected author, timestamp and exact payload.
   verify attachment/caption or the intended state change as applicable. ambiguous
   matches remain uncertain; an older identical message does not verify this send.
6. report actual account, target, action, verification and message ids when available.

a success result alone may omit a message id. find the bounded read-back match;
do not invent an id. a timeout or crash can leave delivery uncertain: inspect the
peer before retrying. preserve duplicate protection; `force_resend=true` requires
explicit resend authority, not an automatic response to a blocked duplicate.

## repeated work

use `tg_create_write_lane` only for an explicit recurring grant covering purpose,
targets, ttl, total/per-target quotas, interval, length, links and any active hours.
compare the inert preview with that exact grant before `tg_approve_write_lane`.
an already explicit matching grant need not be solicited again; missing authority
must be obtained. never widen scope while activating or executing the lane.

lanes authorize text sends only. use `tg_send_message_with_lane`, inspect lane audit
and stop/revoke when complete, expired, out of scope or quota-limited. new targets,
files, forwards, edits, deletes and membership changes require their own authority.
for approved multi-step member or cleanup missions, inspect the repository's batch
tools and lease rules instead of turning a one-off send into a broad write lane.
