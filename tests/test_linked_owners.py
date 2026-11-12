"""Shared companion stores through authenticated native private turns."""
from tests import astrbot_data_root  # noqa: F401

import asyncio
import unittest

from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from companion_v2.execution.models import require
from companion_v2.memory.models import Proposal
from tests.linked_reply_support import LinkedReplyFixture


class LinkedOwnerTests(LinkedReplyFixture, unittest.IsolatedAsyncioTestCase):
    async def turn(self, index, text, identifier):
        event = self.event_for(index, text, identifier)
        await asyncio.wait_for(await self.dispatch(event), 20)
        await self.controller.session.linked_conversations.gate.drain()
        prepared = event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]
        await prepared["native_source_future"]
        return prepared

    def learn(self, prepared, operation):
        context = prepared["context"]
        evidence = self.app.memory.writer.access.capture(context, prepared["native_source_future"].result())
        return require(self.app.memory.writer.submit(context, Proposal("add", predicate="drink",
            object="tea", representation="The owner drinks tea", evidence=(evidence,)), operation_key=operation))

    async def test_existing_fact_and_new_sources_are_shared_without_moving_records(self):
        first = await self.turn(0, "I drink tea", "before-link")
        saved = self.learn(first, "learn-before-link")
        self.assertEqual(saved["state"], "persisted")
        separate = await self.turn(1, "A previously separate fact", "separate-before-link")
        separate_fact = self.learn(separate, "learn-separate-before-link")
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Remember my drink", "after-link")
        context = second["context"]
        from companion_v2.memory.models import AtomRef
        original = require(self.app.memory.writer.read(context, AtomRef(saved["atom_id"], 1)))
        self.assertEqual((original["atom_id"], original["revision"]), (saved["atom_id"], 1))
        excluded = self.app.memory.writer.read(context, AtomRef(separate_fact["atom_id"], 1))
        self.assertEqual(excluded.failure.code, "memory_unavailable")
        self.app.memory.query.begin(context, "shared-facts")
        result = require(self.app.memory.query.query(context, "shared-facts", query_intent="", kind="facts"))
        self.assertIn("The owner drinks tea", str(result))
        source = first["native_source_future"].result()
        self.assertEqual(self.app.memory.writer.access.capture(context, source).source.scope.platform, "qq-main")
        back = await self.turn(0, "What did I ask?", "back")
        other_source = second["native_source_future"].result()
        self.assertEqual(self.app.memory.writer.access.capture(back["context"], other_source).source.scope.platform,
                         "qq-second")

    async def test_link_does_not_inherit_core_tier_or_bypass_endpoint_action_revocation(self):
        from companion_v2.identity_policy.models import AuthorizationTier
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy
        policy = ObjectAccessPolicy(self.app.runtime)
        first = (await self.turn(0, "Hello", "permission-before"))["context"]
        policy.set_tier(first.actor.subject, AuthorizationTier.CORE,
                        expected_revision=policy.revision, observed_at=self.clock.now())
        self.assertEqual((await self.link())["status"], "ok")
        second = (await self.turn(1, "Hello again", "permission-after"))["context"]
        admin = policy.scope_access(second.actor, scope=second.scope, action="access.policy.set",
                                    observed_at=self.clock.now())
        self.assertFalse(admin.is_success)
        self.assertEqual(admin.failure.code, "core_access_required")
        directory = self.controller.authority.identities
        for grant in directory.grants_for_subject(second.actor.subject):
            if grant.scope == second.scope and grant.capability == "memory.read":
                directory.revoke_grant(grant.grant_id, revoked_at=self.clock.now(),
                                       expected_revision=directory.revision)
        with self.assertRaises(Exception) as denied:
            self.app.memory.query.begin(second, "revoked-read")
        self.assertEqual(denied.exception.code, "authorization_revoked")
        self.app.memory.query.begin(first, "original-still-readable")

    async def test_companion_body_and_maintenance_owner_already_agree(self):
        from astrbot_host.management_identity import dashboard_operator
        from companion_v2.role_work.state.ownership import role_schedule_key
        from tests.test_role_state import state_data
        first = await self.turn(0, "I feel rested", "body-before")
        operator = dashboard_operator(self.app.runtime, "synthetic-admin", at=self.clock.now())
        self.app.role_state.write(first["context"], data=state_data(energy=54), expected_revision=0,
            operation_key="body-before-link", sources=(first["native_source_future"].result(),),
            operator=operator, reason="human_edit")
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "How rested?", "body-after")
        body = self.app.role_state.read(second["context"], operator=operator)
        self.assertEqual(body["values"]["energy"], 54)
        self.assertEqual(role_schedule_key(first["context"]), role_schedule_key(second["context"]))
        self.app.role_state.write(second["context"], data=state_data(energy=61), expected_revision=1,
            operation_key="body-from-second", sources=(second["native_source_future"].result(),),
            operator=operator, reason="human_edit")
        self.assertEqual(self.app.role_state.read(first["context"], operator=operator)["values"]["energy"], 61)

    def grant(self, context, action):
        from companion_v2.identity_policy.models import AuthorizationTier
        directory = self.controller.authority.identities
        directory.issue_grant(grant_id="synthetic:" + context.key + ":" + action,
            subject=context.actor.subject, scope=context.scope, capability=action,
            tier=AuthorizationTier.STANDARD, valid_from=self.clock.now(), expected_revision=directory.revision)

    async def test_relationship_memory_already_agrees_and_retains_source_scope(self):
        from companion_v2.foundation.refs import RoleOwnerRef
        from companion_v2.relationships import RelationshipMemory, RelationshipMemoryKind
        first = await self.turn(0, "We enjoy walking together", "relationship-before")
        context = first["context"]
        self.grant(context, "relationship.memory.propose")
        service = self.app.relationship_memories
        original = require(service.propose(RelationshipMemory("shared-relationship",
            RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id),
            context.actor.subject, context.scope, RelationshipMemoryKind.CONTEXT,
            "We enjoy walking together", (first["native_source_future"].result(),), self.clock.now(), self.clock.now()),
            actor=context.actor, observed_at=self.clock.now()))
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Our walks", "relationship-after")
        self.grant(second["context"], "relationship.memory.read")
        shared = require(service.read("shared-relationship", actor=second["context"].actor,
            scope=second["context"].scope, observed_at=self.clock.now()))
        self.assertEqual(shared, original)
        self.assertEqual(shared.sources[0].scope.platform, "qq-main")

    async def test_window_settings_share_values_but_keep_physical_authorization_scope(self):
        from companion_v2.conversation.reply_settings import ReplyConfigurationOwner, configuration_fields, target_for
        first = await self.turn(0, "Preferences", "settings-before")
        owner = ReplyConfigurationOwner(self.app.runtime)
        common = self.controller.settings.common
        fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        first_target = target_for(first["context"])
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Same preferences", "settings-after")
        second_target = target_for(second["context"])
        field = "conversation.presentation.private_debounce_enabled"
        self.assertFalse(owner.read(second_target, fields).fields[field].value)
        self.assertEqual(second_target.authorization_scope.platform, "qq-second")
        owner.write(second_target, fields, changes={field: True}, reset=(),
            expected_revision=owner.read(second_target, fields).revision, observed_at=self.clock.now())
        self.assertTrue(owner.read(first_target, fields).fields[field].value)

    async def test_scope_based_settings_read_the_linked_window_owner(self):
        from companion_v2.conversation.reply_settings import ReplyConfigurationOwner, configuration_fields, target_for
        from companion_v2.conversation.media.understanding import intake_settings
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Shared intake preferences", "intake-settings-a"))["context"]
        second = (await self.turn(1, "Read intake preferences", "intake-settings-b"))["context"]
        common = self.controller.settings.common
        fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        owner, target = ReplyConfigurationOwner(self.app.runtime), target_for(first)
        owner.write(target, fields, changes={"conversation.presentation.file_understanding_max_chars": 12000},
            reset=(), expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        self.assertEqual(intake_settings(self.app.runtime, second.scope, second.role_binding.role)
                         ["file_understanding_max_chars"], 12000)

    async def test_model_window_settings_are_shared_in_dashboard_and_runtime_reads(self):
        from astrbot_host.management_identity import dashboard_operator
        from astrbot_host.model_route_material import route_snapshot
        from companion_v2.model_gateway.management import ModelManagement
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Model preferences", "model-settings-a"))["context"]
        second = (await self.turn(1, "Shared model preferences", "model-settings-b"))["context"]
        operator = dashboard_operator(self.app.runtime, "synthetic-admin", at=self.clock.now())
        manager = ModelManagement(self.app, second)
        status = require(manager.status(operator, layer="window"))
        task = status["tasks"][0]
        saved = require(manager.configure(operator, {"kind": task["kind"], "layer": "window",
            "operation_key": "shared-model-prompt", "edit_token": task["edit_token"],
            "changes": {"prompt": "Shared model task instruction"}, "reset": []}))
        operation = require(self.app.configuration.operations.get(saved["operation_id"]))
        self.assertEqual(operation.operation.scope, second.scope)
        other = require(ModelManagement(self.app, first).status(operator, layer="window"))
        original_task = next(item for item in other["tasks"] if item["kind"] == task["kind"])
        self.assertEqual(original_task["fields"]["prompt"]["value"], "Shared model task instruction")
        self.assertEqual(route_snapshot(self.app, second), route_snapshot(self.app, first))

    async def test_timeline_and_journal_archive_share_records_with_original_sources(self):
        from companion_v2.role_work.growth import GrowthKind, RoleGrowthRecord
        from companion_v2.role_work.models import RoleActivity, RoleActivityState
        from companion_v2.role_work.journal.models import JournalContext, JournalDraft, JournalKey, JournalKind
        first = await self.turn(0, "We walked and wrote a diary", "timeline-before")
        context, at = first["context"], self.clock.now()
        life = self.app.life_context
        owner = life.owner(context)
        activity = life.life.add_activity(RoleActivity("shared-walk", context.role_binding.role.subject_id,
            context.scope, at, RoleActivityState.ACTIVE, description="A walk together"))
        growth = self.app.role_growth_context.reader.ledger.add(RoleGrowthRecord("shared-goal", owner,
            context.scope, GrowthKind.GOAL, "Walk regularly", "Walk together", "Proposed",
            (first["native_source_future"].result(),), at, at, proposed_by=context.actor.subject))
        key = JournalKey(owner, JournalKind.DIARY, at.date().isoformat())
        life.journal.save(JournalDraft(JournalContext(key, context.scope, "UTC"), body="We walked together"),
            expected_revision=0, operation_id="shared-diary")
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Our timeline", "timeline-after")
        reader = second["context"]
        for action in ("role.life.read", "role.growth.read"):
            self.grant(reader, action)
        result = self.app.role_timeline.query(reader, kinds=["activity", "growth", "diary"])
        identifiers = {item["reference"]["identifier"] for item in result["items"]}
        self.assertEqual(identifiers, {"shared-walk", "shared-goal", "shared-diary"})
        archive = life.journal.get_for_audience(key, scope=reader.scope, recipient=reader.actor.subject, observed_at=at)
        self.assertEqual(archive.draft.body, "We walked together")
        self.assertEqual(archive.draft.context.scope.platform, "qq-main")
        self.assertEqual((activity.scope.platform, growth.sources[0].scope.platform), ("qq-main", "qq-main"))

    async def test_usage_unifies_existing_and_linked_turns_once_with_source_tags(self):
        from companion_v2.model_gateway.core_observations import observations_for_role
        first = await self.turn(0, "Count this old turn", "usage-before")
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Count this new turn", "usage-after")
        await asyncio.gather(*self.controller.session.core_reply_observations._pending)
        first_rows = observations_for_role(self.app.runtime, first["context"])
        second_rows = observations_for_role(self.app.runtime, second["context"])
        self.assertEqual({r["turn_id"] for r in first_rows}, {first["turn_id"], second["turn_id"]})
        self.assertEqual(first_rows, second_rows)
        self.assertEqual({r["source_channel"] for r in second_rows}, {"qq-main", "qq-second"})
        duplicate = self.event_for(1, "Count this new turn", "usage-after")
        await (await self.dispatch(duplicate))
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(len(observations_for_role(self.app.runtime, second["context"])), 2)

    async def test_input_logs_are_one_view_with_physical_source_tags(self):
        from astrbot.core.log import LogManager
        sink = LogManager.get_plugin_logger("astrbot_plugin_private_companion")
        self.assertEqual((await self.link())["status"], "ok")
        with self.assertLogs(sink, level="INFO") as captured:
            await self.turn(0, "Log A", "log-a")
            await self.turn(1, "Log B", "log-b")
            duplicate = self.event_for(1, "Log B", "log-b")
            await (await self.dispatch(duplicate))
        rows = [record.getMessage() for record in captured.records if "companion_native_input" in record.getMessage()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("source_channel=qq-main" in row for row in rows))
        self.assertTrue(any("source_channel=qq-second" in row for row in rows))

    async def test_owners_survive_restart_and_original_endpoint_detachment(self):
        from datetime import timedelta
        from companion_v2.model_gateway.core_observations import observations_for_role
        from companion_v2.memory.models import AtomRef
        from companion_v2.role_work.state.ownership import role_schedule_key
        first = await self.turn(0, "I drink tea", "restart-before")
        saved = self.learn(first, "persistent-fact")
        self.clock.advance(timedelta(seconds=2))
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Before restart", "restart-linked")
        await self.controller.terminate()
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        result = await self.controller.start()
        self.assertTrue(result.is_success, result.failure)
        self.app = self.controller.session.application
        restored = await self.turn(1, "After restart", "restart-after")
        self.assertEqual(require(self.app.memory.writer.read(restored["context"], AtomRef(saved["atom_id"], 1)))["object"], "tea")
        self.assertEqual(second["context"].owner_scope, restored["context"].owner_scope)
        self.assertEqual(role_schedule_key(first["context"]), role_schedule_key(restored["context"]))
        self.assertEqual((await self.link(revision=1, members=["second"]))["status"], "ok")
        self.native.platforms["qq-main"].client._wsr_api_clients.clear()
        detached = await self.turn(1, "Only this endpoint remains", "after-detach")
        self.assertEqual(require(self.app.memory.writer.read(detached["context"], AtomRef(saved["atom_id"], 1)))["object"], "tea")
        self.assertEqual({row["source_channel"] for row in observations_for_role(self.app.runtime, detached["context"])},
                         {"qq-main", "qq-second"})

    async def test_unlinked_context_encoding_is_unchanged_and_owner_scopes_cannot_be_forged(self):
        from dataclasses import replace
        import json
        from companion_v2.execution.models import encode
        first = (await self.turn(0, "Independent", "unlinked-encoding"))["context"]
        self.assertNotIn("storage_scope", json.loads(encode(first)))
        forged = replace(first, storage_scope=replace(first.scope, native_scope_id="unrelated", session_id="unrelated"))
        result = self.app.tasks.check_context_binding(forged, observed_at=self.clock.now())
        self.assertFalse(result.is_success)
        self.assertEqual(result.failure.code, "conversation_owner_changed")
        from companion_v2.conversation.reply_settings import GROUP, target_for
        target = replace(target_for(first), storage_scope=forged.storage_scope)
        denied = self.app.configuration.read(first.actor, target=target, group_id=GROUP,
                                             observed_at=self.clock.now())
        self.assertEqual(denied.failure.code, "configuration_owner_changed")

    async def test_schema_one_link_without_subject_uses_persisted_anchor_identity(self):
        from astrbot_host.linked_conversations import KEY
        first = (await self.turn(0, "An existing conversation", "old-link-before"))["context"]
        self.assertEqual((await self.link())["status"], "ok")
        records = self.controller.authority.read(KEY)
        for record in records.values():
            record["anchor"].pop("subject")
        self.controller.authority.write(KEY, records)
        await self.controller.terminate()
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        result = await self.controller.start()
        self.assertTrue(result.is_success, result.failure)
        self.app = self.controller.session.application
        second = (await self.turn(1, "Read the old link", "old-link-after"))["context"]
        self.assertEqual(second.actor.subject, first.actor.subject)
        self.assertEqual(second.owner_scope, first.scope)

    async def test_person_memory_shares_names_with_the_source_channel_of_each_change(self):
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Remember my name", "person-a"))["context"]
        second = (await self.turn(1, "The same person", "person-b"))["context"]
        cards = self.app.messages.acquaintance
        cards.record_platform_names(first, nickname="First channel name")
        view = cards.recall(second)
        self.assertEqual(view["display_name"], "First channel name")
        self.assertEqual(view["display_name_source_channel"], "qq-main")
        cards.record_platform_names(second, nickname="Second channel name")
        view = cards.recall(first)
        self.assertEqual(view["display_name"], "Second channel name")
        self.assertEqual(view["recent_renames"][0]["source_channel"], "qq-second")


class LinkedDistinctPrincipalTests(LinkedOwnerTests):
    origins = ("qq-main:FriendMessage:200", "qq-second:FriendMessage:201")

    def make_controller(self, config=None):
        from tests.multi_host_support import multi_config
        config = config or multi_config()
        config["multi_host"]["targets"][1]["participant_id"] = "201"
        return super().make_controller(config)

    async def test_joining_admin_endpoint_does_not_promote_the_conversation_owner(self):
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy
        policy = ObjectAccessPolicy(self.app.runtime)
        first = (await self.turn(0, "A standard conversation", "standard-owner"))["context"]
        self.controller.authority.core_target_id = "second"
        independent = (await self.turn(1, "An administrator endpoint", "admin-before-link"))["context"]
        self.assertTrue(policy.scope_access(independent.actor, scope=independent.scope,
            action="access.policy.set", observed_at=self.clock.now()).is_success)
        self.assertEqual((await self.link())["status"], "ok")
        joined = (await self.turn(1, "Join the standard conversation", "admin-after-link"))["context"]
        for context in (first, joined):
            result = policy.scope_access(context.actor, scope=context.scope,
                action="access.policy.set", observed_at=self.clock.now())
            self.assertFalse(result.is_success)
            self.assertEqual(result.failure.code, "core_access_required")


class LinkedSameBotIdentityTests(LinkedReplyFixture, unittest.IsolatedAsyncioTestCase):
    origins = ("qq-main:FriendMessage:200", "qq-second:FriendMessage:201")
    turn = LinkedOwnerTests.turn

    def make_controller(self, config=None):
        from tests.multi_host_support import multi_config
        config = config or multi_config()
        config["multi_host"]["targets"][1].update(participant_id="201", bot_id="bot-a")
        config["multi_host"]["personas"] = [row for row in config["multi_host"]["personas"] if row["bot_id"] == "bot-a"]
        return super().make_controller(config)

    async def test_private_link_does_not_rebind_identity_used_by_unlinked_group_routes(self):
        second = await self.turn(1, "An independent native identity", "identity-before")
        original = second["context"].actor
        self.assertEqual((await self.link())["status"], "ok")
        linked = await self.turn(1, "A linked private conversation", "identity-after")
        current = require(self.controller.authority.identities.resolve_authenticated(original.principal,
            observed_at=self.clock.now()))
        self.assertEqual((current.subject, current.binding_id, current.binding_revision),
                         (original.subject, original.binding_id, original.binding_revision))
        self.assertEqual(linked["context"].actor.subject.subject_id, "onebot-user:200")
