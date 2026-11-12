# Explicit private conversation links

Ticket #3 links native **model history** only. Memory, relationships, companion state,
window settings and companion logs keep their existing physical owners (ticket #11).
AstrBot, its event origins, provider/tool runner, history format and transports are unchanged.

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

## Turn lifetime

The shared gate is keyed by the AstrBot CID, independent of link ID/revision. Authenticated
companion intake acquires it before preparation. A high-priority native waiting hook enforces
or reuses it before AstrBot takes its physical origin lock and loads history. One pipeline task
can reuse its permit for the same event; another event cannot borrow admission. Direct linked
preparation without a live permit is refused.

The pipeline task's done callback starts a strongly referenced cleanup task. Cleanup collects
abandoned async generators once, acquires/releases AstrBot's original physical-origin lock,
and only then releases the conversation permit. No hook or timer releases it. Acquisition has
a 15-second busy timeout. Timeout, cancellation, invalid records or stale selections stop the
event explicitly, because AstrBot catches hook exceptions, including cancellation.

The pre-model tripwire never waits for a permit inside the native lock. It checks task
ownership, revision, membership, anchor existence, selection and effective native settings.
Native tool/provider awaits can run in child tasks: tool/send authorization validates their
event's **still-running pipeline owner's** permit, without admitting a child task. Revision,
physical authentication and existing companion permissions are rechecked before external tools
and sends. The gate registry survives import retirement in its own process module; unload waits
for admitted pipeline work, origin barriers and management operations before closing storage.

Linked settings must match at creation and remain equal to the recorded snapshot: native profile
configuration, provider/model, resolved and forced persona, session services and variables.
The provider's mutable `max_context_tokens` cache is excluded because AstrBot fills it during
request construction. Event/handler model overrides that change the selected model are refused.

Parsed chat commands are refused by default on linked endpoints, including third-party commands;
the companion's literal binding/unbinding and full-reset commands are also refused. Native `/stop`
remains available, including during drain, and stops only its own physical origin. Coherent shared
controls are #7. Live Mode is refused because it writes history twice. Native cron/background
history-writing tools are filtered from linked turns. Companion room writers skip linked CIDs
and cannot race first-link creation. Unlinked coordination and reply preparation stay unchanged.

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
- Shared memory/state/settings/logs (#11), Telegram linked dialogue (#4), coherent controls (#7),
  cross-endpoint stop, preferred background delivery and operational recovery are separate work.

## Offline proof

`tests.linked_reply_support` boots the real EventBus, config router/manager, full PipelineScheduler
(all standard stages), real PersonaManager, InternalAgentSubStage, main-agent request/history build,
ToolLoopAgentRunner, FunctionToolExecutor, native lock manager, result decoration/respond stages,
and ConversationManager over temporary SQLite. Companion intake, authorization, tool injection,
native send guards and observations run for real. Handlers are registered through the normal
AstrBot metadata/registry APIs; no core function or registry implementation is patched.

Only provider and platform/transport ports are synthetic. A Context is assembled from those
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
revision/settings drift, restart, revoked origins, busy timeout and unload/reload.

Run SDK tests only with the isolated runner:

```sh
/home/ubuntu/scratch/companion-telegram/run-tests.sh /home/ubuntu/projects/companion-worktrees/t3 \
  python -X utf8 -m unittest -q \
  tests.test_linked_conversation tests.test_astrbot_linked_turn_contract \
  tests.test_core_reply_conversation_ownership tests.test_telegram_private_reply
```

No network, installed plugin, deployment state or running AstrBot container is used.

The [ticket validation report](ticket-3-validation.md) maps every acceptance criterion to its
tests and records the exact commands, baseline failures and verification limits.
