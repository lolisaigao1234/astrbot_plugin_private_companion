# Ticket #3 implementation and validation

Implementation: `60823cda` (`feat: link private endpoints with task-owned conversation permits`),
plus the final startup-state guard and strengthened history/streaming tests on the ticket branch.
No push, PR, issue edit or AstrBot modification was made.

## Acceptance criteria

Methods below are in `tests.test_linked_conversation.LinkedConversationConcurrencyTests`
unless another module is named.

| Criterion | Result | Evidence |
| --- | --- | --- |
| 1. Test-first, synthetic roots, fake external ports, real reply path | Met | `test_negative_control_shared_selection_without_gate_loses_a_turn`; `test_gate_holds_from_preparation_through_final_save`; recorded red/green runs for the gate, management API, tool authorization, startup states, model override and provider cache |
| 2. Explicit versioned management operation; no implicit links; unlinked behavior preserved | Met | `test_explicit_versioned_link_selects_existing_history_and_survives_restart`; `test_management_requires_dashboard_administrator`; `test_unlinked_endpoints_remain_concurrent_with_separate_histories`; unchanged unlinked QQ snapshot in `tests.test_core_reply_conversation_ownership` |
| 3. Existing ongoing history and effective native configuration; physical sources intact; no copying/merging | Met | `test_managed_link_serializes_equal_ids_and_redelivery_has_one_effect`; `test_explicit_versioned_link_selects_existing_history_and_survives_restart` checks that the other, deliberately different old history stays untouched; settings/source-model/override/drift tests |
| 4. Serialize through model/tools, streaming, failure, cancellation and final persistence; unlinked concurrency unchanged | Met | `test_gate_holds_from_preparation_through_final_save`; `test_stopped_yield_defers_finalization_and_barrier_never_admits_early`; buffered/true-streaming, tool/failure, provider error/exception, send/save error, stop, cancellation and busy-timeout tests |
| 5. Near-simultaneous linked turns survive once, in order | Met | `test_managed_link_serializes_equal_ids_and_redelivery_has_one_effect`; negative control loses a turn without the gate |
| 6. Stop if companion-only serialization is impossible | Met; stop condition not triggered | Real-path positive/negative controls and native-origin barrier proof; core source and registry implementation remain unmodified |
| 7. Equal IDs distinct; redelivery has one effect; stale revisions fenced | Met within the existing redelivery window | `test_managed_link_serializes_equal_ids_and_redelivery_has_one_effect`; `test_management_revision_conflict_and_prepared_work_are_fenced`; durable restart redelivery remains explicitly deferred under owner priorities |
| 8. Conversation survives original endpoint detachment/disconnection | Met for conversation ownership | `test_conversation_continues_after_original_endpoint_is_detached_and_offline`; canonical descriptor contains persisted metadata, never the original client |
| 9. Link and shared native selection survive restart | Met | `test_full_restart_restores_link_and_astrbot_public_selection` reconstructs both controller and native ConversationManager; `test_explicit_versioned_link_selects_existing_history_and_survives_restart` also checks stop/start |
| 10. Session-changing/destructive commands unavailable with first link | Met | `test_session_changing_and_unknown_commands_are_blocked_before_handlers`; `test_binding_commands_are_unavailable_from_a_linked_endpoint`; native `/stop`, including during unload, remains covered separately |

## Mechanism and note 04

The conversation permit, pipeline-task ownership, re-entry, explicit stop on admission failure,
native-origin-lock barrier, strong cleanup references, no timed release, reload persistence and
pre-model tripwire follow note 04. Acquisition times out after 15 seconds; release never does.
The coordinator key is the stable native CID, not a link revision.

Two runtime details required care:

1. AstrBot's runner creates child tasks around provider/tool awaits. They can validate their own
   event's live pipeline owner's permit for authorization; only the pipeline task can acquire it.
   The real tool continuation test first returned `linked_revision_changed`, then passed with this
   distinction. Neither the lock order nor the release boundary changed.
2. AstrBot mutates the derived `max_context_tokens` field while building a request, including the
   provider dict retained in native configuration. That cache is removed from both representations
   before settings hashing. Provider/model selection and other configuration remain fenced.

Startup guards additionally keep linked native fallback closed when the host is stopped, cold,
or loaded but not started. Management operations and admitted pipeline work drain before storage
closes. New chat commands are conservatively denied except native `/stop`; literal companion
binding/reset commands are also denied. No dashboard page or configuration-schema change was needed.

## Real and synthetic execution

Real: EventBus dispatch; configuration router/manager; every standard PipelineScheduler stage;
PersonaManager; InternalAgentSubStage; main-agent history/request construction; ToolLoopAgentRunner;
FunctionToolExecutor; native per-origin lock; decoration/respond; native QQ sending code; and
ConversationManager/SQLite history replacement. The companion's preparation, permission checks,
tools, delivery guard and observations are real too.

Synthetic: provider responses and platform/client transport ports; a deliberately failing external
tool; temporary accounts and native/plugin stores. Context services are assembled without booting
AstrBot's full application. The ConversationManager subclass only instruments reads/writes and
adds deliberate save pause/error points; successful operations delegate to real AstrBot SQLite.
Normal handler registration is used, with no replaced core hook dispatcher, agent stage or registry.

The ordinary positive test pauses after model completion and again on entry to final persistence.
B performs no history read/model/tool/write until A's persistence and native lock barrier complete.
The negative control overwrites B's turn. A separate stopped-yield test blocks the barrier after A's
pipeline task finishes. Streaming assertions require the provider's stream method, native RespondStage
stream consumption, and the synthetic transport's individual chunks; presentation features that
normally disable streaming are disabled through the real configuration stores for these cases.

## Commands and results

All SDK invocations use:

```sh
RUNNER=/home/ubuntu/scratch/companion-telegram/run-tests.sh
TREE=/home/ubuntu/projects/companion-worktrees/t3
```

Full feature/SDK run on `60823cda`: **66 tests, OK**, 507.928 seconds:

```sh
"$RUNNER" "$TREE" python -X utf8 -m unittest -q \
  tests.test_linked_conversation tests.test_astrbot_linked_turn_contract \
  tests.test_core_reply_conversation_ownership tests.test_telegram_private_reply
```

The final branch additionally contains the loaded-but-unstarted startup case and stronger
independent-history/streaming assertions. Recheck those final changes with this exact command:

```sh
"$RUNNER" "$TREE" python -X utf8 -m unittest -q \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_explicit_versioned_link_selects_existing_history_and_survives_restart \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_unlinked_endpoints_remain_concurrent_with_separate_histories \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_managed_link_serializes_equal_ids_and_redelivery_has_one_effect \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_negative_control_shared_selection_without_gate_loses_a_turn \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_conversation_continues_after_original_endpoint_is_detached_and_offline \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_buffered_streaming_saves_once_before_successor \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_true_streaming_transport_saves_once_before_successor \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_loaded_but_unstarted_host_refuses_ungated_native_fallback \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_stopped_host_cannot_fall_through_to_an_ungated_native_turn \
  tests.test_linked_conversation.LinkedConversationConcurrencyTests.test_cold_controller_refuses_linked_native_turn_before_host_has_loaded
```

Before the final commit, the distinct-history group passed **5 tests** (50.219 seconds), and the
real streaming group passed **2 tests** (28.825 seconds). The final startup-state group passed
**3 tests** (14.216 seconds). After the final fix commit, the exact ten-case recheck above
passed **10 tests, OK** (83.908 seconds), including actual buffered/true streaming and all three
unavailable-host states.

Regression command (the first 26 suites are the baseline set):

```sh
BASE_SUITES=(
  tests.test_astrbot_multi_host tests.test_multi_binding_sync
  tests.test_multi_role_route_refresh tests.test_multi_target_install_apis
  tests.test_session_topology tests.test_native_host_events
  tests.test_lark_private_route tests.test_lark_platform tests.test_weixin_platform
  tests.test_plugin_event_routing tests.test_core_reply_pipeline
  tests.test_main_core_reply tests.test_main_no_reply tests.test_core_reply_admission
  tests.test_core_reply_decision tests.test_core_reply_closing
  tests.test_core_reply_idempotency tests.test_core_reply_reload
  tests.test_core_reply_turn_gate tests.test_core_reply_native_send
  tests.test_private_debounce tests.test_astrbot_core_reply
  tests.test_conversation_context_reset tests.test_reset_command
  tests.test_binding_command tests.test_functional_commands
)
# Baseline launched before implementation from integration tip 9d92d8ec:
"$RUNNER" "$TREE" python -X utf8 -m unittest -q "${BASE_SUITES[@]}"
# Post-change regression, including the required ownership/Telegram tests:
"$RUNNER" "$TREE" python -X utf8 -m unittest -q "${BASE_SUITES[@]}" \
  tests.test_core_reply_conversation_ownership tests.test_telegram_private_reply \
  tests.test_reality_together_native
```

Baseline: **431 tests; 7 failures, 12 errors** (1209.109 seconds).
Post-change: **466 tests; the same 7 failures, 12 errors** (1303.535 seconds).
Comparison of all failure/error test names and subtests found no additions or removals.
The broad run precedes the final one-condition startup guard; the final targeted run covers it.

The unchanged baseline failures are:

- `test_lark_platform`: missing `DEFAULT_PLATFORM_NAMES` (1 error); bot-open-ID group mention (1 failure).
- `test_plugin_event_routing`: all 8 error entries, including the two sign-in subtests, due to missing
  `SimpleNamespace.display_name` in the existing fixture.
- `test_main_no_reply`: both StartupRetryTests lack `_qzone_life_publisher` (2 errors).
- `test_conversation_context_reset`: third-party native reset marker test lacks `context.conversation_manager` (1 error).
- `test_main_core_reply`: face-only private follow-up test (1 failure).
- `test_binding_command`: five EventIdentityTests retain the existing account-ID expectation mismatch
  (5 failures, including the `self_id=None` subtest).

Required local checks:

```sh
cd /home/ubuntu/projects/companion-worktrees/t3
git diff --check
python3 -X utf8 -m tools.check_tracked_privacy
```

Both pass. `pages/` and `_conf_schema.json` are untouched, so generated-page synchronization is
not applicable. The integration tip remained `9d92d8ec` at the final check and was already an ancestor,
so no integration merge was needed.

## Deliberately unverified or deferred

No live accounts, network sends, deployment state, installed plugin or running AstrBot container
were used. Durable redelivery across a process restart, concurrent Dashboard/external history
writers, and cross-process coordination are not claimed. The companion must stay enabled on
linked sessions. Startup with a configured but absent original adapter retains the existing
channel-startup coupling tracked separately; conversation ownership itself no longer depends on
that adapter. Shared memory/state/settings/logs, coherent unlink/new/reset controls, cross-endpoint
stop and Telegram-linked rollout remain the designated follow-up tickets.
