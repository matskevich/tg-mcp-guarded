# private broadcast channels

`tg_create_private_channel(title, expected_actor_id)` creates an empty private
broadcast channel. use only under an owner instruction to create that channel.
allowlist its exact title in the ActionMCP process. the existing account,
confirmation phrase, one-time preview approval and process guards apply.

creation reserves idempotency before the rpc and disables transport retries.
unknown outcomes require reading dialogs before any further action. there is no
force-resend parameter. acceptance checks the returned channel is creator-owned,
private, broadcast, and has both signatures and signature profiles disabled.

adding members and granting publication remain separate guarded operations.
verify the exact participant set and each publishing role through the independent
read runtime. membership alone does not confer publication rights. telegram may
add effective administrator rights to the requested role; follow the publishing
tool's explicit authority requirement before accepting those additional rights.

new processes of the installed ActionMCP launcher discover the tool without a
service restart. no new credential consumer or direct Telethon write path is used.

## shared owner catalog

`mcp_private_channel_provider.py` exposes the same creation arguments and a
read-only account status to the existing `agent-infra` entry. this adapter uses
the installed `op-agents` profile, then scopes the exact title in the child env.
preview/confirmation arguments pass unchanged; it never approves an action itself.
only channel creation and account status can be dispatched through this adapter.

publish these two exact tools on the owner resource `telegram-channels`, with
separate grants for existing owner consumers. preserve their other resources.
clients use discover → describe → status → creation preview, then an owner-authorized
native confirmation. delivery tests can stop at preview; creating a smoke channel
requires its own owner instruction. previously opened native tool lists may stay
cached; shared discovery reads the current catalog on every call.
