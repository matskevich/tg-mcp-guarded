# native rich publication

status: source and isolated native read/preview checked. actual rich writes and
the existing cloud plugin catalog require separate consumer acceptance. this is
not permission to post, create a test channel or change the owner's copy.

the provider pins telethon 1.46.0 (tl layer 233). new rich actors opt into
`TG_EXPLICIT_CURRENT_LAYER=1`; existing consumers need their own rollout. the
explicit layer envelope resolves inner request fields first and preserves the
direct-write guard, including nested transport wrappers. forum operations use
the compatibility adapter for the channels-to-messages namespace migration.

## read

- `tg_get_full_message(group, message_id, text_offset, text_limit)` returns the
  full raw message, entities, rich blocks, media references and reply metadata.
  follow `next_text_offset` until null and check `content_complete`.
- `projection_complete=false` means a text projection is unsupported. the raw
  tree remains present; it is not an empty successful message. binary references
  are tagged base64, not decoded as utf8.
- `tg_get_discussion_root(channel, channel_post_id)` matches the forwarded
  channel identity and original post id. use its discussion peer and root id;
  the channel post number is not necessarily the discussion message number.

## exact rich payload

the current write input is a native block tree. this preserves source text and
broken lines without an implicit markdown/html parser, truncation or splitting:

```json
{"_":"InputRichMessage","blocks":[{"_":"PageBlockParagraph","text":{"_":"TextPlain","text":"exact copy\n\nincluding its final newline\n"}}]}
```

styling uses native nested rich text constructors such as `TextBold`. preserve
the source blocks and ordered media references. unsupported constructors fail
explicitly. a raw tree is not evidence that every media/layout variant has been
accepted by a real client; text, formatting and multimedia need scoped scenarios.

## guarded send

`tg_send_rich_message` requires the exact numeric target, stable `operation_id`,
expected actor, expected displayed sender and explicit `send_as_id`. normal
ActionMCP allowlist, account, approval, confirmation and process guards remain.
dry-run binds the entire payload, sender and reply identity. a linked comment
additionally binds `discussion_channel`, `channel_post_id` and the independently
resolved `reply_to_msg_id`. ordinary replies must exist in the exact target.

a reviewed staging actor can set `TG_RICH_PREVIEW_ONLY=1`. it then rejects
execution before a reservation or rpc, even when a valid preview exists.
preparation does not grant live-test or publication authority.

the operation is durably reserved before the rpc and records the target's
pre-dispatch message watermark. mutations have no automatic transport retry.
reuse the same operation id after an unknown response, never a new id or force
flag. `sent` is not effect acceptance. independently read the complete message
and use `tg_reconcile_rich_message` to match the exact new message, actor,
displayed sender, blocks, media order and reply root. an older identical message
cannot satisfy the watermark. unknown/mismatched effects remain fenced.

private deployment and test receipts belong in the owning infrastructure issue.
roll back the new actor/catalog route only; preserve existing actors, sessions,
ordinary sends and unknown operation records. deleting test messages is a
separate exact guarded action, not an implicit rollback.

### saved messages sender and rejected-attempt recovery

for a verified self target with actor and displayed sender equal to that self
id, the native request omits `send_as`: telegram rejects an explicit sender on
saved messages. the exact actor/sender remains in the approved payload and full
readback. other target identities still use the explicit requested sender.

`SendAsPeerInvalidError` is a definite provider rejection, recorded durably as
`rejected`; no message was created by that RPC. a fresh exact preview and one-time
approval are required before a same-operation attempt. timeouts and all other
unclassified errors retain their unknown reservation and cannot be retried here.
concurrent claim of a rejected attempt is atomic; it cannot dispatch twice.

for historical receipts from versions that recorded this specific rejection as
unknown, the operator may use `scripts/reconcile_rich_rejection.py` with the
trusted private native receipt, exact operation id and existing ledger. it checks
error type, action hash, unsent state and absence of a known message, and retains
the receipt hash in `rejection_receipts`. it makes no telegram call and cannot
release a timeout, changed payload or sent operation. receipt paths remain private.

### existing rich replies and server normalization

`tg_get_full_message` accepts optional `thread_root_id` (default0 preserves old
calls). when ordinary ids lookup misses a reply, the reader uses bounded native
`messages.getReplies` for that exact peer/root/id window, then checks the actual
message id, peer and reply/top root. provenance names the retrieval method.
reconciliation uses the operation's approved reply identity automatically.

server implicit `TextAutoUrl` wrappers and adjacent plain `TextConcat` runs may
normalize the submitted tree. only those representation differences are folded
for equality; exact text/order, explicit links, styles, blocks, sender, reply,
media order and rtl still must match. raw trees and their actual hashes remain
unchanged; reconciliation reports exact-tree match separately from the narrow
normalization policy. no send/edit/delete is part of reading or reconciliation.

### read-only missing-message diagnostics

`tg_get_message_admin_events` reads a bounded admin delete/edit window, returns
message content only for exact requested ids, and reports current moderation/ttl
settings without changing them. actual admin-log access is required. absence of
a matching audit entry is not proof of no deletion. native anti-spam being enabled
is not proof that it removed a particular message.

optional `from_pts` adds one bounded `updates.getChannelDifference` read filtered
to the exact message range. returned messages and deletion ids are filtered to
the requested ids. preserve raw update type/pts/count: a deletion signal carries
no actor or event time, so a later intentional cleanup must not be mistaken for
the cause of an earlier failed delivery. no polling/subscription loop is installed.

future send acknowledgements persist only matching id/message updates in the
private durable ledger, excluding unrelated users/chats. provider acceptance is
still not useful delivery. this cannot retrospectively recover raw updates that
an older source version discarded. raw send evidence, independent current read
and actual recipient/UI usefulness remain separate acceptance stages.
