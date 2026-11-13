# Explicit private conversation links

Explicit links share native model history and the companion owners described below.
AstrBot, its event origins, provider/tool runner, history format and transports are unchanged.
Notes, reminders and the proactive schedule are not shared yet; see the boundaries below.

## Management

Use the existing authenticated AstrBot Dashboard account/session API:

`POST /astrbot_plugin_private_companion/accounts/conversation-link`

```json
{
  "schema_version": 1,
  "link_id": "owner-dialogue",
  "expected_revision": 0,
  "conversation_id": "existing-native-conversation-id",
  "source_target_id": "existing-owner-window",
  "member_target_ids": ["existing-owner-window", "another-exact-private-window"]
}
```

The caller must be a signed-in Dashboard administrator; API-key identities are refused.
The source must already select this existing conversation. Members must be exact configured
private targets, with no legacy primary/role window link. Names, equal native IDs, persona
names and wildcard admission templates never establish a link. No old history is imported,
copied, merged or rekeyed.

The returned document has `schema_version: 1` and an incremented `revision`. Account API GET
includes `conversation_links`. To replace membership, POST the same link ID and conversation
ID, the current `expected_revision`, the original `source_target_id`, and the complete desired
member list. A stale revision is refused. Changing the conversation anchor is deferred to
lifecycle controls (#7).

An existing turn or companion room history writer makes the operation busy. During creation,
new work is refused on the affected origins. The operation verifies quiescence behind native
origin locks, saves its document in runtime metadata (`astrbot.private-conversation-links.v1`),
and uses AstrBot's public `switch_conversation()` for selection. AstrBot persists each selection;
the companion persists the independent conversation anchor and member identities. If interrupted
between these steps, selection validation refuses turns until management retries with the current
revision. It never silently creates a replacement conversation.

The original endpoint need not remain connected or in the member list. The anchor retains
conversation/role metadata rather than a live endpoint dependency. Detached endpoints remain
refused, including when the initial source is omitted from membership. They cannot continue
unguarded against the shared CID. Fresh independent admission/unlink controls belong to #7.

## Shared companion owners

The persisted anchor records the existing conversation's logical bot, role, human subject
and storage scope. Older link records resolve the human through that anchor's persisted
route. An older linked endpoint whose exact route already records a different human
cannot silently adopt another recipient; that conflict is refused rather than rewriting
historical provenance. Owner resolution never needs the first endpoint's live client. Linking neither
copies the joining endpoint's old records nor rewrites either endpoint's history.

- Long-term facts, relationship memory, the role body and its maintenance key use their
  existing bot/role/human owners. Person cards use the anchor's storage scope.
- Window settings are stored once, at the anchor. The address is resolved where configuration
  rows are keyed (the sparse settings owner and the configuration service), not by each
  feature: a target always carries the physical window, so authorization, the grants a
  settings write issues or revokes, and operation receipts stay with the endpoint that acted.
  Role and relationship settings retain their existing owners.
- Timeline, journal and life-context readers include the linked physical scopes, also for an
  entry that carries its original input. Original activity, evidence, journal and operation
  records keep their source scope.
- The reply background of every linked endpoint includes the adopted relationship records of
  the conversation, whichever endpoint they were recorded on.
- The life routines (daily diary, sleep) have one owner per linked conversation: the first
  endpoint that binds, and after a restart the first one that is available. The routine runs
  under that endpoint's own context, grant and provenance; the other endpoints adopt it.
- The native per-role usage view covers the conversation's own linked scopes without changing
  stored usage keys, so another participant of the same account is not counted. Each native
  turn appears once and includes `source_channel`. The AstrBot plugin logger records one
  `companion_native_input` line per admitted input with its turn ID and physical source channel.

## Permissions of a linked endpoint

Identity and stored owners are shared across a link. Authority is not.

- The task's authentication, audience, transport and source references remain physical.
  Additional endpoints use a separate private principal binding; the native binding used by
  unrelated conversations and groups is preserved.
- An endpoint that joined a conversation never has more than the standard tier, whatever tier
  the shared human subject holds. Every tier check uses the one helper that knows this
  (`access_tier`), including interventions, core-only tools and commands, relay, screen and
  device control. Dashboard administration remains a separate authentication path.
- Every permission decision uses the endpoint's own grants and revocations. When a shared
  record names another member's scope, the reader is still judged on its own scope. A joined
  endpoint reaches only the scopes of its current link; any other scope is refused before a
  grant is looked at, including other conversations of the same human.
- The features a private window's owner has without a grant (relationship and calendar
  reading, notes, self-review, expression reading) apply on each endpoint's own window.
- What an endpoint had been refused in its own conversation stays refused when it joins: the
  refusals are recorded on its linked scope before any default grant is issued. Grants it held
  are not copied; the linked scope starts from the same defaults as any new conversation.
- The pre-send check re-reads the joined endpoint's own dialogue permission, so a revocation
  made during a turn stops that turn's reply.

The link record is decoded once per stored value. An endpoint in no link is never affected by
an unreadable or malformed record; an endpoint known to be a member stays closed until the
record can be read again. Without an explicit link, owner resolution leaves existing addresses
and stored record forms unchanged.

This does **not** yet unify notes, reminders, non-role calendar records, the proactive and
reminder schedule, proactive contact preferences' legacy fallbacks, or window budget accounts.
Their owners are coupled to physical delivery or exact-scope accounting and belong to #5 and
#6. Exact-window diagnostic/legacy usage views also remain physical. A detached anchor
endpoint keeps the owner it natively holds; revoking that belongs to #7.
The plugin's existing Dashboard log-buffer behavior is unchanged; the tagged input log
is available through AstrBot's plugin logging. Independent-channel startup permits the
remaining endpoint to restart while the original platform is absent; the memory and
archive still resolve through the stored anchor.

## Telegram private text as a linked endpoint

A Telegram private chat that is already an exact companion private target is linked like any
other endpoint: name its target in `member_target_ids`. Nothing Telegram-specific is configured
and the companion adds no poller, webhook or Telegram call. AstrBot's Telegram channel receives,
formats, splits and sends, and AstrBot's whitelist still decides which chats reach any plugin.

- The conversation the Telegram chat held while it was independent stays stored as it was. It is
  no longer selected and nothing from it is merged into the shared conversation.
- A turn on either channel loads the one shared history and its reply returns only to the chat
  the message arrived on. Equal message numbers on QQ and Telegram are different inputs; a
  redelivered input is admitted once.
- The Telegram endpoint checks (a human's private text outside topics, the exact target, the
  current running application and client) run together with the link's revision and membership:
  on arrival, before tools and before each send. A private topic in the linked chat shares the
  chat's origin and is refused, as is a message from a stopped or replaced application.
- Linking does not widen the tools of the Telegram endpoint. The companion offers it only
  `turn.evidence`, as on an unlinked Telegram endpoint; QQ-only actions are not offered. AstrBot's
  own tools stay as AstrBot offers them for that chat, and its `send_message_to_user` remains
  limited to the chat the message arrived on. Further text tools belong to #5.
- A Telegram send that raises is recorded as `unknown` and is not sent again on either channel.
  AstrBot has stored the reply in the shared history by then, as it does on QQ.
- Either channel can be disconnected or removed. The other keeps the conversation, including
  across a host restart.

## Turn lifetime

The shared gate is keyed by the AstrBot CID, independent of link ID/revision. Authenticated
companion intake acquires it directly before reply preparation, and only for the message that
survived the private duplicate check, follow-up fold and debounce: rapid messages from one
linked chat merge exactly as in an unlinked chat, and no message waits for a sibling while
holding the permit. Identity and debounce settings are read before the permit; that step never
opens a replacement conversation on a linked endpoint. A high-priority native waiting hook
enforces or reuses the permit before AstrBot takes its physical origin lock and loads history.
One pipeline task can reuse its permit for the same event; another event cannot borrow
admission. Direct linked preparation without a live permit is refused.

The pipeline task's done callback starts a strongly referenced cleanup task. Cleanup collects
abandoned async generators once, acquires/releases AstrBot's original physical-origin lock,
and only then releases the conversation permit. No hook or timer releases it. A waiting turn
waits without a time limit, as an unlinked chat's next message waits on AstrBot's own per-origin
lock. Cancellation, invalid records or stale selections stop the event explicitly, because
AstrBot catches hook exceptions, including cancellation.

The pre-model tripwire never waits for a permit inside the native lock. It checks task
ownership, revision, membership, anchor existence, selection and effective native settings.
Native tool/provider awaits can run in child tasks: tool/send authorization validates their
event's **still-running pipeline owner's** permit, without admitting a child task. Revision,
physical authentication and existing companion permissions are rechecked before external tools
and sends. The gate registry survives import retirement in its own process module; unload waits
for admitted pipeline work, origin barriers and management operations before closing storage.

Linked members must agree with each other, at creation and on every turn, on what AstrBot
resolves per origin and that shapes the turn: the local agent runner and its settings (history
compression, fallback models, step and tool limits), provider settings, the selected provider and
model, the resolved and forced persona, the companion's enablement, and session services and
variables. Nothing is frozen at link time: a setting changed for all
members is followed, a difference between members refuses turns until they agree again, and
unrelated configuration (whitelist, admins, platforms, the provider list) is ignored.
Event/handler model overrides that change the selected model are refused.

AstrBot's own session-changing commands (`/reset`, `/new`, `/provider`, `/set`, `/unset`) and the
companion's literal binding/unbinding and full-reset commands are refused on linked endpoints.
Other plugins' commands and AstrBot's remaining commands pass untouched. Native `/stop`
remains available, including during drain, and stops only its own physical origin. Coherent shared
controls are #7. Live Mode is refused because it writes history twice. Native cron/background
history-writing tools are filtered from linked turns. Companion room writers skip linked CIDs
and cannot race first-link creation. Unlinked coordination and reply preparation stay unchanged.

Linked origins are indexed in memory. An event from any other origin, including every group
event, returns from the companion's link hooks without reading the link record. While the host
is not running, linked origins stay refused from that index; a new process reads the one record
once. If that read fails, the exact private targets in the companion's configuration, the only
chats that can be link members, are refused until the record can be read or the host runs; group
chats and chats that are not companion targets keep AstrBot's own replies.

## Boundaries and follow-ups

- Requires the pinned AstrBot 4.29.4 local runner, its `session_lock_manager.acquire_lock()` utility,
  and one event loop. The contract tests deliberately fail when these assumptions change.
- Keep the companion enabled for every linked session. Link creation and each admitted turn
  verify enabled waiting hooks. Disabling the plugin itself removes its enforcement; native
  plugin disablement cannot be intercepted by an unloaded plugin.
- AstrBot Dashboard conversation edits, externally scheduled cron jobs, other plugins' direct
  history writes and other processes are outside this coordinator. Do not edit linked history
  concurrently through those paths. Drift detected on the next turn is refused.
- Native redelivery protection reuses the existing in-memory 120-second/512-entry window, scoped
  by physical origin and message ID, and survives plugin import reload. Durable redelivery
  across a process restart remains deferred per the owner's priorities.
- Send exceptions retain existing unknown-outcome accounting. There is no automatic replay.
- On a joining endpoint the open-item section is available but lists that endpoint's own
  notes: notes and reminders are not shared yet (#5, #6).
- Further Telegram text tools (#5), coherent controls (#7), cross-endpoint stop, preferred
  background delivery (#6) and operational recovery remain work.

## Offline proof

`tests.linked_reply_support` boots the real EventBus, config router/manager, full PipelineScheduler
(all standard stages), real PersonaManager, InternalAgentSubStage, main-agent request/history build,
ToolLoopAgentRunner, FunctionToolExecutor, native lock manager, result decoration/respond stages,
and ConversationManager over temporary SQLite. Companion intake, authorization, tool injection,
native send guards and observations run for real. Handlers are registered through the normal
AstrBot metadata/registry APIs; no core function or registry implementation is patched.

Only provider and platform/transport ports are synthetic. For Telegram these are the
application and its client; the event is AstrBot's `TelegramPlatformEvent` with the channel's
own metadata and session sender. A Context is assembled from those
services without starting AstrBot's application lifecycle. The manager subclass adds read/write
instrumentation and explicit pause/failure points, delegating every actual read and successful
write to AstrBot. Streaming uses the real provider/runner/respond path: QQ's adapter buffers;
the synthetic streaming transport additionally consumes chunks as they arrive. A synthetic
failing external tool exercises the real tool executor's exception path.

The negative control selects one CID without a gate and loses A's turn. The positive test pauses
A in the real post-model response hook and at final persistence, then starts B: B reads/calls/writes
nothing until A's save and the native barrier complete. Both turns are then stored once, in order,
and replies retain their physical origins. A stopped-yield test holds the native barrier after
A's task finishes and proves that B stays blocked. Other cases cover streaming, tools/failure,
provider error/exception, send/save errors, stop, cancellation while waiting/running/saving,
revision drift, member settings divergence and recovery, members with different history limits,
an unrelated configuration edit, debounced rapid messages, a turn queued past 15 seconds, other
plugins' commands, restart, revoked origins, a stopped host, a new process whose link record
cannot be read, and unload/reload. Each ordering case waits until the second turn is queued on
the permit and checks that it loads no history, so it fails if the gate stops serializing.

`tests.test_telegram_linked_conversation` repeats the continuation, ordering, redelivery,
fencing, send-failure and shared-owner cases with the joining endpoint on Telegram, and adds
its tool boundary and each channel running without the other.

Run these suites with an AstrBot SDK environment that uses a temporary data root, never an
installed plugin or a live data directory:

```sh
python -X utf8 -m unittest -q \
  tests.test_linked_owners tests.test_linked_conversation tests.test_astrbot_linked_turn_contract \
  tests.test_core_reply_conversation_ownership tests.test_telegram_private_reply \
  tests.test_telegram_linked_conversation
```

No network, installed plugin, deployment state or running AstrBot container is used.
