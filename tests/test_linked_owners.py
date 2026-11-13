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

    async def attempt(self, index, text, identifier):
        """A turn that may be refused: its prepared context, or None."""
        event = self.event_for(index, text, identifier)
        await asyncio.wait_for(await self.dispatch(event), 20)
        await self.controller.session.linked_conversations.gate.drain()
        return (event.get_extra(CORE_REPLY_CONTEXT_EXTRA) or {}).get("prepared")

    def revoke(self, context, capability):
        directory = self.controller.authority.identities
        for grant in directory.grants_for_subject(context.actor.subject):
            if grant.scope == context.scope and grant.capability == capability:
                directory.revoke_grant(grant.grant_id, revoked_at=self.clock.now(),
                                       expected_revision=directory.revision)

    async def test_a_joined_endpoint_gains_no_tier_from_the_shared_subject(self):
        from astrbot_host.command_authority import is_core_subject
        from companion_v2.capabilities.privacy import is_core
        from companion_v2.identity_policy.interventions import INTERVENTION_CAPABILITIES, authorized_capabilities
        from companion_v2.identity_policy.models import AuthorizationTier
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy
        policy = ObjectAccessPolicy(self.app.runtime)
        first = (await self.turn(0, "Hello", "tier-before"))["context"]
        policy.set_tier(first.actor.subject, AuthorizationTier.CORE,
                        expected_revision=policy.revision, observed_at=self.clock.now())
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Hello", "tier-owner"))["context"]
        second = (await self.turn(1, "Hello again", "tier-joined"))["context"]
        self.assertEqual(second.actor.subject, first.actor.subject)
        at = self.clock.now()
        self.assertEqual(authorized_capabilities(self.app.tasks, first, at=at), frozenset(INTERVENTION_CAPABILITIES))
        self.assertEqual(authorized_capabilities(self.app.tasks, second, at=at), frozenset())
        self.assertEqual((is_core(self.app.runtime, first.actor), is_core(self.app.runtime, second.actor)), (True, False))
        self.assertEqual((is_core_subject(self.app.runtime, first.actor),
                          is_core_subject(self.app.runtime, second.actor)), (True, False))
        session = self.controller.session
        self.assertEqual((session._actor_is_core(first), session._actor_is_core(second)), (True, False))

    async def test_a_joined_endpoint_is_fenced_to_the_scopes_of_its_link(self):
        from companion_v2.conversation.reply_settings import GROUP
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy
        await self.db.create_conversation(self.origins[0], "qq-main", cid="conversation-c", persona_id="original",
                                          content=[])
        await self.manager.switch_conversation(self.origins[0], "conversation-c")
        unrelated = (await self.turn(0, "Another conversation of the owner", "fence-unrelated"))["context"]
        await self.manager.switch_conversation(self.origins[0], "conversation-a")
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "The linked conversation", "fence-owner"))["context"]
        second = (await self.turn(1, "The joined endpoint", "fence-joined"))["context"]
        self.assertNotEqual(unrelated.scope.conversation_id, first.scope.conversation_id)
        policy, at = ObjectAccessPolicy(self.app.runtime), self.clock.now()
        self.assertTrue(policy.scope_access(first.actor, scope=unrelated.scope, action="memory.read",
                                            observed_at=at, allow_private_owner=True).is_success)
        for scope in (first.scope, second.scope):
            self.assertTrue(policy.scope_access(second.actor, scope=scope, action="memory.read",
                                                observed_at=at, allow_private_owner=True).is_success)
        refused = policy.scope_access(second.actor, scope=unrelated.scope, action="memory.read",
                                      observed_at=at, allow_private_owner=True)
        self.assertEqual(refused.failure and refused.failure.code, "scope_not_granted")
        settings = self.app.configuration.read(second.actor, target=self.window(unrelated), group_id=GROUP,
                                               observed_at=at)
        self.assertEqual(settings.failure and settings.failure.code, "scope_not_granted")

    async def test_a_refusal_made_before_linking_still_binds_the_endpoint(self):
        second = (await self.turn(1, "An independent endpoint", "refusal-before"))["context"]
        self.revoke(second, "memory.read")
        self.assertFalse(await self.attempt(1, "Refused while independent", "refusal-independent"))
        self.assertEqual((await self.link())["status"], "ok")
        self.assertFalse(await self.attempt(1, "Refused after linking", "refusal-linked"))
        self.assertTrue(await self.attempt(0, "The other endpoint is unaffected", "refusal-other"))

    async def test_a_joined_endpoint_keeps_owner_features_on_its_own_window_only(self):
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy
        policy = ObjectAccessPolicy(self.app.runtime)
        actions = ("relationship.memory.read", "calendar.read", "calendar.write", "memo.read", "memo.list",
                   "role.self_review.read", "role.expression.read")
        from dataclasses import replace
        def owned(context, scope):
            return {action for action in actions if policy.feature_access(context.actor, scope=scope, action=action,
                observed_at=self.clock.now(), allow_private_owner=True).is_success}
        independent = (await self.turn(1, "Before linking", "owner-feature-before"))["context"]
        before = owned(independent, independent.scope)
        self.assertEqual(before, set(actions))
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "The owner", "owner-feature-owner"))["context"]
        second = (await self.turn(1, "After linking", "owner-feature-joined"))["context"]
        for context in (first, second):
            for scope in (first.scope, second.scope):
                self.assertEqual(owned(context, scope), before)
        outside = replace(second.scope, conversation_id="astrbot-conversation:another")
        self.assertEqual(owned(second, outside), set())

    async def test_pre_send_recheck_refuses_a_joined_endpoint_that_lost_dialogue_permission(self):
        self.assertEqual((await self.link(source_target_id="second", conversation_id="conversation-b"))["status"], "ok")
        self.pause_model = True
        event = self.event_for(0, "Still allowed?", "presend-joined")
        task = await self.dispatch(event)
        await asyncio.wait_for(self.model_done.wait(), 20)
        prepared = event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]
        self.assertEqual(prepared["context"].actor.principal.host_id, "astrbot-native-linked-private")
        session, target = self.controller.session, self.controller.target_for_event(event)
        self.assertTrue((await session.authorize_core_reply(event, target, prepared)).is_success)
        self.revoke(prepared["context"], "conversation.reply")
        refused = await session.authorize_core_reply(event, target, prepared)
        self.assertEqual(refused.failure and refused.failure.code, "private_access_dialogue_denied")
        self.allow_model.set()
        await asyncio.wait_for(task, 20)
        await session.linked_conversations.gate.drain()

    async def test_companion_body_and_maintenance_owner_already_agree(self):
        from astrbot_host.management_identity import dashboard_operator
        from companion_v2.role_work.state.ownership import role_schedule_key
        from tests.test_role_state import state_data
        from companion_v2.storage.repositories import OperationRepository
        operations = OperationRepository(self.app.runtime)
        first = await self.turn(0, "I feel rested", "body-before")
        operator = dashboard_operator(self.app.runtime, "synthetic-admin", at=self.clock.now())
        saved = self.app.role_state.write(first["context"], data=state_data(energy=54), expected_revision=0,
            operation_key="body-before-link", sources=(first["native_source_future"].result(),),
            operator=operator, reason="human_edit")
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "How rested?", "body-after")
        body = self.app.role_state.read(second["context"], operator=operator)
        self.assertEqual(body["values"]["energy"], 54)
        self.assertEqual(require(operations.get(saved["operation_id"])).operation.scope.platform,
                         "qq-main")
        self.assertEqual(role_schedule_key(first["context"]), role_schedule_key(second["context"]))
        saved = self.app.role_state.write(second["context"], data=state_data(energy=61), expected_revision=1,
            operation_key="body-from-second", sources=(second["native_source_future"].result(),),
            operator=operator, reason="human_edit")
        updated = self.app.role_state.read(first["context"], operator=operator)
        self.assertEqual(updated["values"]["energy"], 61)
        self.assertEqual(require(operations.get(saved["operation_id"])).operation.scope.platform,
                         "qq-second")

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
        shared = require(service.read("shared-relationship", actor=second["context"].actor,
            scope=second["context"].scope, observed_at=self.clock.now()))
        self.assertEqual(shared, original)
        self.assertEqual(shared.sources[0].scope.platform, "qq-main")

    @staticmethod
    def sections(prepared):
        return {item["purpose"]: item["state"] for item in prepared["context_materials"]}

    async def test_reply_background_of_a_joined_endpoint_has_the_owner_sections_and_the_shared_relationship(self):
        from companion_v2.foundation.refs import RoleOwnerRef
        from companion_v2.relationships import RelationshipMemory, RelationshipMemoryKind, RelationshipMemoryState
        first = await self.turn(0, "We enjoy walking together", "background-before")
        context, service = first["context"], self.app.relationship_memories
        for action in ("propose", "review", "adopt"):
            self.grant(context, "relationship.memory." + action)
        memory = require(service.propose(RelationshipMemory("adopted-relationship",
            RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id),
            context.actor.subject, context.scope, RelationshipMemoryKind.CONTEXT,
            "We enjoy walking together", (first["native_source_future"].result(),), self.clock.now(), self.clock.now()),
            actor=context.actor, observed_at=self.clock.now()))
        for state in (RelationshipMemoryState.REVIEWED, RelationshipMemoryState.ADOPTED):
            memory = require(service.transition(memory.memory_id, actor=context.actor, scope=context.scope,
                expected_revision=memory.revision, operation_id="adopted-relationship:" + state.value, state=state,
                observed_at=self.clock.now()))
        self.assertEqual((await self.link())["status"], "ok")
        owner = await self.turn(0, "Our walks", "background-owner")
        joined = await self.turn(1, "Our walks again", "background-joined")
        for purpose in ("relationship", "open_items"):
            self.assertNotEqual(self.sections(owner)[purpose], "unavailable")
            self.assertEqual(self.sections(joined)[purpose], self.sections(owner)[purpose])
        self.assertEqual(self.sections(joined)["relationship"], "ready")
        reader = self.app.messages.relationship_context
        snapshot = reader.capture(joined["context"], self.clock.now())
        self.assertEqual([item["memory_id"] for item in snapshot["references"]], ["adopted-relationship"])
        self.assertIn("We enjoy walking together",
                      str(reader.contributions(snapshot, joined["context"], self.clock.now())))

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

    @staticmethod
    def window(context):
        """A window target as every feature builds it: the physical scope and nothing else."""
        from companion_v2.configuration.models import ConfigLayer, ConfigTarget
        return ConfigTarget(context.scope.installation_id, ConfigLayer.WINDOW, context.role_binding.role, context.scope)

    async def test_plain_window_targets_address_the_conversation_settings(self):
        from companion_v2.conversation.reply_settings import GROUP, ReplyConfigurationOwner, configuration_fields
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Plain target", "plain-target-a"))["context"]
        second = (await self.turn(1, "Plain target again", "plain-target-b"))["context"]
        common = self.controller.settings.common
        fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        owner, field = ReplyConfigurationOwner(self.app.runtime), "conversation.presentation.private_debounce_enabled"
        target = self.window(second)
        owner.write(target, fields, changes={field: True}, reset=(),
            expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        self.assertTrue(owner.read(self.window(first), fields).fields[field].value)
        snapshot = require(self.app.configuration.read(first.actor, target=self.window(first), group_id=GROUP,
                                                       observed_at=self.clock.now()))
        self.assertTrue(snapshot.fields[field].value)
        self.assertEqual(snapshot.target.authorization_scope, first.scope)

    async def test_settings_read_through_the_configuration_side_store_are_shared(self):
        from companion_v2.role_work.life.configuration import LifeConfigurationOwner, configuration_fields
        from companion_v2.role_work.life.rest_gate import read_settings
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Rest preferences", "side-store-a"))["context"]
        second = (await self.turn(1, "Rest preferences again", "side-store-b"))["context"]
        self.assertIsNot(self.app.runtime.configuration_store, self.app.runtime)
        owner, target, fields = LifeConfigurationOwner(self.app.runtime), self.window(first), configuration_fields()
        owner.write(target, fields, changes={"role.life.rest_reply_probability": 41}, reset=(),
            expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        self.assertEqual(read_settings(self.app.runtime, second).rest_reply_probability, 41)

    async def test_a_settings_write_changes_grants_only_on_the_endpoint_that_wrote_it(self):
        from companion_v2.role_work.growth.configuration import GrowthConfigurationOwner, configuration_fields
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Growth preferences", "growth-grants-a"))["context"]
        second = (await self.turn(1, "Growth preferences again", "growth-grants-b"))["context"]
        owner, fields, field = GrowthConfigurationOwner(self.app.runtime), configuration_fields(), "role.growth.read_enabled"
        directory = self.controller.authority.identities
        for context in (first, second):
            target = self.window(context)
            owner.write(target, fields, changes={field: True}, reset=(),
                expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        def allowed(context):
            return directory.authorize(require(directory.resolve_authenticated(context.actor.principal,
                observed_at=self.clock.now())), scope=context.scope, capability="role.growth.read",
                observed_at=self.clock.now()).is_success
        self.assertEqual((allowed(first), allowed(second)), (True, True))
        target = self.window(second)
        owner.write(target, fields, changes={field: False}, reset=(),
            expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        self.assertFalse(owner.read(self.window(first), fields).fields[field].value)
        self.assertEqual((allowed(first), allowed(second)), (True, False))

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

    async def test_diary_with_its_original_input_and_life_candidates_are_read_through_the_other_endpoint(self):
        from companion_v2.role_work.models import RoleActivity, RoleActivityState
        from companion_v2.role_work.journal.models import JournalContext, JournalDraft, JournalKey, JournalKind
        first = await self.turn(0, "We walked and I wrote it down", "life-before")
        context, at, life = first["context"], self.clock.now(), self.app.life_context
        source = first["native_source_future"].result()
        life.life.add_activity(RoleActivity("evidenced-walk", context.role_binding.role.subject_id,
            context.scope, at, RoleActivityState.ACTIVE, description="A walk together"))
        key = JournalKey(life.owner(context), JournalKind.DIARY, at.date().isoformat())
        life.journal.save(JournalDraft(JournalContext(key, context.scope, "UTC"), body="We walked together",
            sources=(source,)), expected_revision=0, operation_id="evidenced-diary")
        self.assertEqual((await self.link())["status"], "ok")
        reader = (await self.turn(1, "What did we do?", "life-after"))["context"]
        self.grant(reader, "role.life.read")
        entry = life.journal.get_for_audience(key, scope=reader.scope, recipient=reader.actor.subject, observed_at=at)
        self.assertEqual((entry.draft.body, entry.draft.sources[0].scope.platform), ("We walked together", "qq-main"))
        candidates = life.candidates(reader, self.clock.now())
        found = {(item.reference["kind"], item.reference.get("activity_id") or item.reference.get("local_date"))
                 for options in candidates.variants for item in options}
        self.assertEqual(found, {("activity", "evidenced-walk"), ("journal", key.local_date)})
        self.assertEqual(candidates.skipped, 0)

    async def test_linked_usage_excludes_other_participants_of_the_same_account(self):
        from dataclasses import replace
        from companion_v2.foundation.refs import AudienceRef
        from companion_v2.model_gateway.core_observations import observations_for_role, record_observation
        first = await self.turn(0, "Count this turn", "usage-own")
        await asyncio.gather(*self.controller.session.core_reply_observations._pending)
        own = first["context"]
        scope = replace(own.scope, native_scope_id="999", session_id="999")
        stranger = replace(own, role_binding=replace(own.role_binding, scope=scope),
                           audience=AudienceRef(scope, own.audience.recipients))
        mine = observations_for_role(self.app.runtime, own)[0]
        record_observation(self.app.runtime, stranger, {**{key: mine[key] for key in mine
            if key not in ("context_key", "context_json")}, "turn_id": "core-reply:another-participant"})
        self.assertEqual({row["turn_id"] for row in observations_for_role(self.app.runtime, own)},
                         {first["turn_id"], "core-reply:another-participant"})
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Count this one too", "usage-linked")
        await asyncio.gather(*self.controller.session.core_reply_observations._pending)
        for context in (second["context"], (await self.turn(0, "And again", "usage-owner"))["context"]):
            self.assertNotIn("core-reply:another-participant",
                             {row["turn_id"] for row in observations_for_role(self.app.runtime, context)})
        self.assertIn(first["turn_id"],
                      {row["turn_id"] for row in observations_for_role(self.app.runtime, second["context"])})

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
        from companion_v2.role_work.journal.models import JournalContext, JournalDraft, JournalKey, JournalKind
        first = await self.turn(0, "I drink tea", "restart-before")
        saved = self.learn(first, "persistent-fact")
        life, at = self.app.life_context, self.clock.now()
        key = JournalKey(life.owner(first["context"]), JournalKind.DIARY, at.date().isoformat())
        life.journal.save(JournalDraft(JournalContext(key, first["context"].scope, "UTC"), body="Persistent diary"),
                          expected_revision=0, operation_id="persistent-diary")
        self.clock.advance(timedelta(seconds=2))
        self.assertEqual((await self.link())["status"], "ok")
        second = await self.turn(1, "Before restart", "restart-linked")
        await self.controller.terminate()
        self.native.platforms.pop("qq-main")
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        result = await self.controller.start()
        self.assertTrue(result.is_success, result.failure)
        self.app = self.controller.session.application
        restored = await self.turn(1, "After restart", "restart-after")
        self.assertEqual(require(self.app.memory.writer.read(restored["context"], AtomRef(saved["atom_id"], 1)))["object"], "tea")
        from companion_v2.conversation.linked_ownership import owner_scope
        self.assertEqual(owner_scope(self.app.runtime, restored["context"].scope), first["context"].scope)
        self.assertEqual(role_schedule_key(first["context"]), role_schedule_key(restored["context"]))
        self.assertEqual((await self.link(revision=1, members=["second"]))["status"], "ok")
        detached = await self.turn(1, "Only this endpoint remains", "after-detach")
        self.assertEqual(require(self.app.memory.writer.read(detached["context"], AtomRef(saved["atom_id"], 1)))["object"], "tea")
        archive = self.app.life_context.journal.get_for_audience(key, scope=detached["context"].scope,
            recipient=detached["context"].actor.subject, observed_at=self.clock.now())
        self.assertEqual(archive.draft.body, "Persistent diary")
        self.assertEqual(archive.draft.context.scope.platform, "qq-main")
        self.assertEqual({row["source_channel"] for row in observations_for_role(self.app.runtime, detached["context"])},
                         {"qq-main", "qq-second"})

    def life_routines(self, *contexts):
        life = self.app.life_runtime
        return {id(routine): routine for routine in (life.for_context(context) for context in contexts)}

    async def test_linked_endpoints_have_one_life_routine_and_daily_schedule_also_after_restart(self):
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "Good morning", "life-owner-a"))["context"]
        second = (await self.turn(1, "Good morning again", "life-owner-b"))["context"]
        await asyncio.gather(*tuple(self.controller.session.core_reply_maintenance._pending))
        routines = self.life_routines(first, second)
        self.assertEqual(len(routines), 1)
        self.assertEqual({routine.schedule_id for routine in routines.values()},
                         {"role.life.daily:" + first.key})
        await self.controller.terminate()
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        result = await self.controller.start()
        self.assertTrue(result.is_success, result.failure)
        self.app = self.controller.session.application
        contexts = [self.controller.authority.context(target, first.role_binding.role.subject_id)
                    for target in ("first", "second")]
        routines = self.life_routines(*contexts)
        self.assertEqual(len(routines), 1)
        self.assertIsNotNone(next(iter(routines.values())).instruction)

    async def test_context_records_keep_their_stored_form(self):
        from dataclasses import asdict
        import json
        from companion_v2.execution.models import encode
        first = (await self.turn(0, "Independent", "unlinked-encoding"))["context"]
        self.assertEqual((await self.link())["status"], "ok")
        second = (await self.turn(1, "Linked", "linked-encoding"))["context"]
        for context in (first, second):
            # Task records store ``asdict(spec)``; other owners store ``encode(context)``.
            self.assertEqual(set(asdict(context)), {"actor", "role_binding", "audience"})
            self.assertEqual(set(json.loads(encode(context))), {"actor", "role_binding", "audience"})

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
        from companion_v2.conversation.linked_ownership import owner_scope
        self.assertEqual(owner_scope(self.app.runtime, second.scope), first.scope)

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

    async def test_anchor_endpoint_cannot_use_private_ownership_to_bypass_its_action_grant(self):
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy
        self.assertEqual((await self.link())["status"], "ok")
        first = (await self.turn(0, "First source", "anchor-permission-a"))["context"]
        second = (await self.turn(1, "Second source", "anchor-permission-b"))["context"]
        directory = self.controller.authority.identities
        for grant in directory.grants_for_subject(first.actor.subject):
            if grant.scope == first.scope and grant.capability == "memory.read":
                directory.revoke_grant(grant.grant_id, revoked_at=self.clock.now(),
                                       expected_revision=directory.revision)
        policy = ObjectAccessPolicy(self.app.runtime)
        for scope in (first.scope, second.scope):
            result = policy.scope_access(first.actor, scope=scope, action="memory.read",
                allow_private_owner=True, observed_at=self.clock.now())
            self.assertFalse(result.is_success)
            self.assertEqual(result.failure.code, "authorization_revoked")

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
