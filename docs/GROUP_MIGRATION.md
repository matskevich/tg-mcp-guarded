# Basic group migration

`tg_migrate_basic_group(group, expected_title, dry_run=true, confirm=false,
confirmation_text="", approval_code="")` upgrades an existing basic group.
It does not create a replacement group or change username, members, rights,
allowlists, lanes, or automation configuration. The migration is irreversible.

Require an explicit negative basic-group ID and exact current title. Standard
ActionMCP account/process/allowlist/confirmation/approval gates apply. Preview
verifies administrator rights and collects participant IDs without a write RPC.
Execution uses rate-limited `messages.MigrateChatRequest`; this request is also
classified as a write by the transport guard. A reserved attempt cannot be
force-retried through this tool. Reconcile timeout/verification failure first.

Success requires the returned old-to-new mapping, reverse migration mapping,
private supergroup status, unchanged title and identical participant IDs.
A failed postcheck returns `migration_submitted=true` and the new ID when known;
that does NOT mean the mutation was rolled back. Existing migration mapping is
reported without another write. Concurrent membership changes may require manual
reconciliation rather than a success claim.

After success, inspect the new group and update exact automation targets under
owner authority. Preserve old history with old message IDs; create and verify a
new pinned guide in the supergroup for usable links. Do not silently expand a
persistent write lane or make a group public.

VPS28Oct2026: fresh ActionMCP bridge exposes the tool. Read-only live preview for
Awank health correctly refused: Dima account lacks administrator rights. No
migration performed. Long-running MCP clients must reconnect to discover the new
tool; fresh bridge processes load it immediately.
