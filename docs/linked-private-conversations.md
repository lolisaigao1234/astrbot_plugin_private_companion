# Explicit private conversation links

Explicit links share native model history and the companion owners described below.
AstrBot, its event origins, provider/tool runner, history format and transports are unchanged.
Notes, reminders and calendar records share storage. Reminders and proactive text leave through
one preferred endpoint; replies stay on the endpoint a message arrived on.

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
member list. A stale revision is refused. The conversation ID in the document is the one
the members currently select: AstrBot's `/new` from a member replaces it and increments the
revision (see "Shared controls"), so read the current document before editing.

The optional `preferred_target_id` names the member that receives the conversation's
reminders and proactive text; it must be one of `member_target_ids`. A new link starts with
its Telegram member, else the endpoint it was created on, else its first member. A later
operation that does not name it leaves it unchanged. Each link in the account API GET carries
`preferred_target_id` and `background_destination`: `selected`; `missing` while the named
target is not a member or is no longer a configured target; `unavailable` while it is both and
its account is not connected. Text that comes due in the last two states is left unsent with
its reason (see "Reminders and proactive text"), until the account returns or an
administrator selects another member.

An existing turn or companion room history writer makes the operation busy. During creation,
new work is refused on the affected origins. The operation verifies quiescence behind native
origin locks, saves its document in runtime metadata (`astrbot.private-conversation-links.v1`),
and uses AstrBot's public `switch_conversation()` for selection. AstrBot persists each selection;
the companion persists the independent conversation anchor and member identities. If interrupted
between these steps, selection validation refuses turns until management retries with the current
revision. It never silently creates a replacement conversation.

The original endpoint need not remain connected or in the member list. The anchor retains
conversation/role metadata rather than a live endpoint dependency.

## Removing an endpoint

Post the link again without the endpoint in `member_target_ids`. The operation needs the same
quiescence as any link edit, so a turn in flight makes it busy; nothing is copied or rekeyed.

- The endpoints that remain keep the conversation, its whole history, memory, settings and
  records. Records the removed endpoint contributed stay where they are, with their source.
- The removed endpoint is listed under `detached` and is closed: its messages and session
  commands are refused, and no routine is started under it. It no longer selects the shared
  conversation; AstrBot's `new_conversation()` gives it a new, empty one, and nothing of the
  shared transcript is copied there.
- Membership, not the storage key, decides what an endpoint reads. The membership index knows
  the removed endpoints, so every permission decision and every saved task context of a removed
  endpoint is refused (`conversation_link_revoked`, or `scope_not_granted` from a direct grant
  check). This includes the endpoint the conversation started on, whose native bot, role and
  human keys are the shared owner's.
- The revision is incremented. Replies prepared, tools running and sends pending under an
  earlier revision are refused, also after the endpoint is linked again: relinking is a new
  revision and revives nothing.
- A removed endpoint is used on its own only after an administrator admits it: post the link
  with `"independent_target_ids": ["that-target"]` beside the unchanged member list. It then
  leaves the link record, answers with its own native identity and continues in the new, empty
  conversation.
- The endpoint the conversation started on is not admitted independently
  (`linked_anchor_endpoint_closed`): its native identity is the conversation's stored owner, so
  beside the link it would read what the conversation learns later. It can be linked again.
- Routines a running host had bound to the removed endpoint (diary, sleep, state maintenance,
  proactive source maintenance) are refused from then on; a remaining endpoint takes them over
  at the next host start. Restart the companion host after removing the endpoint that was bound
  first. Reminders and proactive contact are not bound to an endpoint this way and need no
  restart (see "Reminders and proactive text").
- A removed endpoint is not a member of the link record, is never the destination of
  background text, and no send is authorized for it.
- The last endpoint cannot be removed. An edit with an empty member list is refused with
  `conversation_link_last_endpoint` and changes nothing: the conversation is reached only
  through its endpoints, so link another private endpoint first. The endpoint the conversation
  started on has no special standing here; it can leave while another member remains (see
  "Retiring the endpoint the conversation started on").

## Shared companion owners

The persisted anchor records the existing conversation's logical bot, role, human subject
and storage scope. Older link records resolve the human through that anchor's persisted
route. An older linked endpoint whose exact route already records a different human
cannot silently adopt another recipient; that conflict is refused rather than rewriting
historical provenance. Owner resolution never needs the first endpoint's live client. Linking neither
copies the joining endpoint's old records nor rewrites either endpoint's history.

- Long-term facts, relationship memory, the role body and its maintenance key use their
  existing bot/role/human owners. Person cards use the anchor's storage scope.
  Read-only legacy memory queries use the existing companion's explicit import mapping at
  that same anchor; imported rows keep their original channel mapping and historical status.
- Notes and reminder records use the anchor's storage key. Both endpoints can list, read,
  edit, complete or reopen notes, cancel or reschedule reminders and look up agenda receipts.
  Existing records are read and edited in place. Each note retains its creator and physical
  target; a reminder retains its physical target when the other endpoint changes it. Object
  checks use that trusted source target with the authenticated caller's own permissions.
  Storage ownership does not select a delivery endpoint; see the preferred endpoint below.
- Non-role calendar reports and commitments use the same conversation owner. Calendar reads,
  revisions and cancellation include records from both linked source scopes without moving
  old records. Cancelling or revising an event can cancel its reminder from either endpoint,
  subject to the caller's reminder permissions. Evidence and operation scopes stay physical.
- Growth tools list records from both linked scopes in one ordered, paginated view. A pending
  proposal can be revised from the other endpoint; each revision keeps its actual evidence.
  Confirmed growth in reply context uses the same shared view and rechecks the reader's grants.
- Text diagnostics inspect the shared settings with the requesting endpoint's permissions.
  Repair proposals can be read and cancelled from either endpoint; their original review stays
  intact. A review seals its originating actor's edit token, so a peer sees it as stale and must
  obtain a fresh local review before confirming a configuration change. Diagnostic observations
  and short-lived report tokens remain tied to the endpoint that performed the check.
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

Owner lookups use an in-memory index of the link record. It is read when the host that
manages links opens the store and again whenever that host writes the record, so a lookup
costs no query and a store without that host is never queried. An endpoint in no link is not
affected by an unreadable or malformed record; an endpoint known to be a member stays closed
until the record can be read again. Without an explicit link, owner resolution leaves
existing addresses, stored record forms and query counts unchanged.

This does **not** unify proactive contact preferences' legacy fallbacks or window budget
accounts, which stay with exact-scope accounting. Exact-window diagnostic/legacy usage views also remain physical.
The plugin's existing Dashboard log-buffer behavior is unchanged; the tagged input log
is available through AstrBot's plugin logging. Independent-channel startup permits the
remaining endpoint to restart while the original platform is absent; the memory and
archive still resolve through the stored anchor.

## Reminders and proactive text: the preferred endpoint

A linked conversation has one reminder schedule, one proactive schedule and one delivery
ledger. Its reminders and proactive text go to one member, the preferred endpoint, and to no
other.

- The destination is decided when a text is dispatched, before anything is sent: the link
  record names the member, and the text is addressed to that member's own context (its
  account, its identity, its role binding on the linked scope). The delivery records that
  concrete destination and the link revision it was resolved from. A reminder made before
  linking, or made or moved on the other endpoint, takes the same route.
- Authority is never borrowed. The endpoint the work was recorded on still needs its own
  permission for it, as before. The destination needs its own permission for the same kind
  of text (`reminder.deliver`, `proactive.contact`) and for dialogue, a registered and
  connected account, and its configured target. Immediately before the platform call these
  are checked again, together with the link: the same member must still be the preferred
  one. The link revision is recorded with the route but is not the fence, because it also
  advances when the members start a new conversation together (`/new`); pending reminders
  and proactive text are kept across that.
- Once dispatch has begun the attempt keeps its destination. Selecting another member affects
  texts that are dispatched afterwards; nothing already sent, failed or uncertain is sent again.
- Nothing is broadcast and no other member is tried. A destination that refuses, or whose
  account is unavailable, leaves the text unsent: the delivery is `cancelled` with the
  refusal code as its reason (for example `authorization_revoked`, `host_account_changed`)
  when it is refused before dispatch, or `failed` with the sender's code in its receipt when
  it is refused at the platform call (`delivery_route_changed` when another member was
  chosen, or the destination left, in between). A send that raises is `unknown` and is never repeated. `delivered` means the
  channel accepted the send; it says nothing about the human having read it.
- When the preferred member is removed from the link and no other is named, the conversation
  has no destination (`background_destination: missing`). A removed endpoint is never a
  destination, also not while it waits in the link's removed list. Texts that come due are recorded as
  `cancelled` with `delivery_destination_missing`; the reminder, its later occurrences and
  its history stay. This state is part of the stored link, so a restart keeps it. After an
  administrator selects a member, later texts go there; earlier ones are not sent.
- A reminder whose time was last set from an endpoint that has since been removed is not
  delivered (`blocked`, `scope_not_granted`): the removed endpoint's work is revoked
  with it. The record stays shared, and setting its time again from a remaining member makes
  it that member's work, also when the record was made on the removed endpoint's window.
  An occurrence that was already blocked is not sent afterwards.
- Proactive contact is planned for one window of the conversation: the one with its most
  recent turn. The other windows wait, and a contact planned on one endpoint is withdrawn
  when the human speaks on another. Only current members count: when the window that
  carried the schedule is removed, a remaining member's window carries it from the next
  pass, with no restart. Idle time, unanswered contacts, cooldown, budgets and
  event history are those of that window. Preferences (master switch, quiet hours, pauses)
  are the conversation's and apply on every endpoint; consent to proactive contact is each
  endpoint's own.
- A linked Telegram destination receives the text only. A QQ poke or a voice plan sealed
  with a contact is dropped, no voice is rendered, and a text that carries an image is not
  sent (`telegram_text_only`). With QQ as the destination the existing QQ voice, poke and
  image behaviour is unchanged. Unlinked conversations, on any channel, are not affected.

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
- Telegram uses the existing authorized text services: memory, notes/reminders, calendar,
  growth/relationship proposals, timeline/source reads, configured weather/news and text diagnostics.
  The same catalogue applies to independent Telegram chats, subject to each endpoint's grants
  and service configuration. QQ poke, QZone, status, media and merged forwards are excluded.
  AstrBot's own tools stay as AstrBot offers them for that chat, and its `send_message_to_user`
  remains limited to the chat the message arrived on.
- Deterministic calendar commands authenticate the Telegram message and send their text receipts
  through AstrBot's session transport. A replaced client invalidates a pending receipt.
- Receiver text replies become separate governed sources, available from `turn.evidence` and
  `source.read`, and remain separate from the human's current text in native history. Whole and
  partial quotes retain the physical account/chat/message/sender and original time. Bot attribution
  uses the connected bot's numeric ID. The local snapshot budget is 8000 characters, with an
  explicit truncation flag; it is not a Telegram transport limit. Nested, remote and inaccessible
  messages supply no additional source or history access. Existing AstrBot quote understanding
  and Telegram text splitting/formatting stay in AstrBot.
- A Telegram send that raises is recorded as `unknown` and is not sent again on either channel.
  AstrBot has stored the reply in the shared history by then, as it does on QQ.
- Either channel can be disconnected or removed. The other keeps the conversation, including
  across a host restart.

## Retiring the endpoint the conversation started on

The conversation belongs to the companion, not to the endpoint it started on. That endpoint
(QQ in the first deployment) can be disconnected, removed from the link, or removed from
configuration altogether, and the remaining endpoint (Telegram) keeps the conversation: its
history, memory, relationships, state, settings, schedule and logs. In none of these states
is anything copied, rekeyed or reset. The stored anchor keeps naming the original endpoint as
the owner address; records it contributed keep it as their source and are read by the
remaining endpoint under that endpoint's own permissions. No start-up, turn, tool, reminder or
proactive text of the remaining endpoint needs the retired endpoint's client, platform or
configuration.

Do it in this order:

1. Make sure the remaining endpoint is the preferred one: the link shows its target as
   `preferred_target_id` with `background_destination: selected`. Otherwise post the link
   with that `preferred_target_id`.
2. From the remaining endpoint, set again the time of every reminder that was last set from
   the endpoint being retired (list the reminders, then reschedule each). While that endpoint
   is still a member they are delivered through the preferred endpoint as they are; once it is
   removed they are `blocked / scope_not_granted` until a member sets their time again, which
   also works after the removal.
3. Remove it from the link: post the link with the remaining endpoint as the only member. The
   revision is incremented and everything prepared, running or unsent under the removed
   endpoint is refused from then on.
4. Remove its account, targets and persona rows from the account mapping and save. First
   check that the remaining account's own persona row names the conversation's role id (the
   `role` in the link's `anchor`): a row that only matched it because the mapping merged two
   generated role ids of one persona stops matching when the other row is deleted. The
   remaining endpoint then still answers, but its routines, reminders and proactive text are
   refused (`host_persona_changed`, a reminder ends `failed`) until the row names that role
   id. The save
   restarts the host with the remaining account only; a link member is not provisioned again
   on its own native scope, so a mapping save with links in place starts as any other.

What each state looks like:

| State of the original endpoint | The remaining endpoint | Reminders and proactive text | What the administrator sees |
| --- | --- | --- | --- |
| Disconnected, still linked and configured | Replies, text tools and commands, controls and settings as before. When the client returns it continues the same conversation, including one started with `/new` meanwhile. | Go to the preferred endpoint. If the disconnected one is preferred: `background_destination: unavailable`, and a text that comes due is `cancelled / host_account_changed`; select another member and later texts go there. Reminders it had set are still delivered through the preferred endpoint. | Host status keeps `running` and accepting; its row under `accounts` has `available: false` with `host_account_unavailable` or `host_platform_unavailable`. |
| Removed from the link | Keeps the conversation and every record. | Its reminders are blocked until set again (step 2). The proactive schedule is carried by a remaining member's window from the next pass. If it was preferred and no member was named: `background_destination: missing`, `cancelled / delivery_destination_missing`. | The link lists it under `detached`; its messages get the "shared conversation unavailable" notice and start no turn. |
| Removed from configuration | Starts and runs with one identity, one set of owners and one schedule. | As above. If it is still a stored member and preferred: `background_destination: missing`, `cancelled / delivery_destination_missing`. | `accounts` lists only the configured accounts. A link edit can name only configured targets (`host_target_unavailable` otherwise), so the next edit leaves it out, which removes it from the link. |

A member that is still stored in the link takes part in the per-turn agreement check with its
stored AstrBot selection and settings, also while it is disconnected or no longer configured.
Take a retired endpoint out of the link (step 3) rather than leaving it there: a later
difference in what AstrBot stores for its origin would refuse the remaining endpoint's turns
(`linked_settings_mismatch`, `linked_selection_changed`) until it is removed.

After that:

- The life routines and state maintenance run under the remaining endpoint with that
  endpoint's own grants.
- QQ-only capabilities (poke, QZone, QQ status, merged forwards, OneBot history and recall)
  are not offered to a Telegram turn and are not attempted for it.
- The retired endpoint cannot be admitted independently (`linked_anchor_endpoint_closed`).
  It, or any other exact private target, is linked later through the normal operation: it
  must be a configured target again, the link gets a new revision, the endpoint selects the
  same conversation and reads what was learned meanwhile, and nothing prepared or left unsent
  under its earlier membership is revived.

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

Live Mode is refused because it writes history twice. Native cron/background
history-writing tools are filtered from linked turns. Companion room writers skip linked CIDs
and cannot race first-link creation. Unlinked coordination and reply preparation stay unchanged.

## Shared controls

AstrBot 4.29.4 keys its session settings by origin and has no shared-session configuration, so
the companion does not reimplement its commands: a member's command runs unchanged, by
AstrBot's own handler and under AstrBot's own permission filter for that physical sender, inside
the conversation permit. A command therefore waits for the turn that is being saved, and no
turn loads history while it runs. Afterwards, still inside the permit, the companion makes the
other members agree with the endpoint that ran the command, through AstrBot's public setters.

| Control | On a linked endpoint |
| --- | --- |
| `/new` | First stops what is in flight on every member, as AstrBot does on its own origin. AstrBot creates the conversation and keeps the previous one; the link record takes the new conversation ID with a new revision and every member selects it. |
| `/reset` | First stops what is in flight on every member. AstrBot clears the one shared history. |
| `/set`, `/unset` | The session variables of the endpoint that ran the command become those of every member. |
| `/provider` | AstrBot's administrator check applies to the physical sender. The chat provider then selected becomes that of every member (`provider_manager.set_provider`). Speech providers stay per channel. |
| `/stop` | Not queued. It stops its own origin as before and also asks the turns running on the other members to stop. |
| `陪伴 全部重置` | Runs inside the permit with its own rules: installation core tier of the physical endpoint (a joined endpoint never has it), preview, a confirmation code bound to the memory revision. It forgets the relationship's memory, which the members share, and ends the temporary context of every member's window. It is not a context reset: AstrBot's history is untouched. |
| Binding and unbinding commands | Refused, as before. |
| Other plugins' commands, AstrBot's remaining commands | Untouched. |

`/new` and `/reset` leave long-term memory alone. They end the companion's temporary window
context for every endpoint of the link, as they do for one window when unlinked.

AstrBot has no persona or model chat command at this version. The conversation's persona is
stored with the conversation and is therefore one value; a persona forced per session, a
configuration profile or a provider chosen per origin in the Dashboard still has to be set
alike for every member, and a difference refuses turns (`linked_settings_mismatch`) until a
control or the Dashboard aligns them. A control is checked on its own origin only, so it can
repair such a difference. If AstrBot's handler fails halfway, the members may disagree; turns
are then refused, never forked, until the next control or link edit.

Sharing a conversation grants nothing: AstrBot administrator status follows the physical
sender, companion tiers and grants follow the physical endpoint, and other plugins (the stack
updater among them) see the physical event as before.

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
- The open-item section and text tools see the linked conversation's notes and reminder records.
  Changing a record does not change where it is delivered; the link's preferred endpoint does.
- When the human moves from one endpoint to another, the proactive schedule moves with them
  and continues with the new window's own contact history: a daily contact allowance or an
  event already shared on the previous endpoint is not counted on the new one.
- The QQ proactive platform circuit is recorded for the account the contact was planned on.
- While the member that had the conversation's latest turn is disconnected or no longer
  configured, but still linked, no proactive contact is composed for the conversation: its
  window carries the schedule and its account is needed to compose. The schedule moves with
  the human's next message on another member, or at once when that endpoint is removed from
  the link.
- A text that came due while the preferred endpoint was unavailable is recorded and not sent
  later; there is no retry when the account returns.
- A joined endpoint has the standard tier. When the endpoint that held the installation core
  tier is retired, no chat endpoint holds it: core-only chat commands and interventions stay
  refused, and administration goes through the Dashboard.
- Link management does not look at the account mapping. Removing the configured target of
  the last linked endpoint there leaves the conversation stored and linked but unreachable
  until that target is configured again.
- Operational recovery, and the live check with a channel actually stopped, remain work.

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

`tests.test_linked_controls` links QQ and Telegram and registers AstrBot's own command
handlers. It removes either endpoint, including the one the conversation started on, and
checks what the removed endpoint can still read, select and start, admission to independent
use, relinking and a restart. Each control is sent from one endpoint while a turn of the
other is paused: the command stays queued on the permit and changes nothing until that turn
has unwound.

`tests.test_telegram_alone` builds one conversation on QQ and Telegram and then retires QQ three
ways: its client disconnected (also across a restart without its channel), the endpoint
removed from the link, and its account, targets and personas removed from the account mapping.
In each state it continues on Telegram: the same history, a fact, a note, a diary entry and a
setting that QQ contributed with QQ still recorded as their source, the text tools, a calendar
command, a session variable, `/new`, a reminder and a proactive text. It also covers the
preferred endpoint being the one that is gone, reminders last set from QQ, the fences on QQ's
earlier work, relinking QQ, the refusal to remove the last endpoint, and a start with no QQ
channel configured.

Run these suites with an AstrBot SDK environment that uses a temporary data root, never an
installed plugin or a live data directory:

```sh
python -X utf8 -m unittest -q \
  tests.test_linked_owners tests.test_linked_conversation tests.test_astrbot_linked_turn_contract \
  tests.test_core_reply_conversation_ownership tests.test_telegram_private_reply \
  tests.test_telegram_linked_conversation tests.test_telegram_text_tools tests.test_linked_text_stores \
  tests.test_linked_controls tests.test_telegram_alone
```

No network, installed plugin, deployment state or running AstrBot container is used.
