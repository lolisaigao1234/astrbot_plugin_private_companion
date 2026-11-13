"""Routed background delivery keeps every check the unrouted delivery has.

The existing reminder and proactive-contact suites run again with their
conversation linked, so each delivery takes the preferred-endpoint route.
"""

from dataclasses import asdict
import unittest

from companion_v2.conversation.linked_ownership import LINKS_KEY, link_records_changed
from companion_v2.execution.models import TaskContext, TaskError, encode
from companion_v2.foundation.refs import AudienceRef
from companion_v2.identity_policy.models import AuthorizationTier
from companion_v2.adapters.astrbot.staging_config import REQUIRED_CAPABILITIES
from tests import test_agenda_background as reminders
from tests import test_proactive_contacts as contacts
from tests.proactive_support import ContactFixture
from tests.test_management_access import ROLE, SCOPE


def member(target_id, scope):
    return {"target_id": target_id, "platform_id": scope.platform, "account_id": scope.account_id,
            "participant_id": scope.native_scope_id, "bot_id": scope.bot_id, "platform_name": "aiocqhttp"}


class LinkedConversation:
    """The fixture's own window is the anchor of a link; ``preferred`` names where its text goes."""

    preferred = None  # a scope other than the fixture's own, when a test routes elsewhere
    link_revision = 1

    def link(self, runtime):
        members = [member("own", SCOPE)] + ([member("other", self.preferred)] if self.preferred else [])
        record = {"schema_version": 1, "link_id": "link", "revision": self.link_revision,
                  "conversation_id": "conversation", "anchor": {"target_id": "own", "scope": SCOPE.document(),
                                                                "role": asdict(ROLE)},
                  "members": members, "detached": [], "preferred_target_id": members[-1]["target_id"]}
        with runtime.transaction() as connection:
            connection.execute("""INSERT INTO runtime_metadata VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (LINKS_KEY, encode({"link": record})))
        link_records_changed(runtime)

    def destination(self, link, endpoint):
        """The host's port: the preferred member's own current context."""
        scope = self.preferred or SCOPE
        return TaskContext(self.important, self.identity.resolve_role(scope, observed_at=self.clock.now()).unwrap(),
                           AudienceRef(scope, (self.important.subject,)))

    def host(self, **options):
        host = super().host(**options)
        self.link(host.application.runtime)
        host.application.outbound.destination = self.destination
        return host


class LinkedReminderChecks(LinkedConversation, reminders.AgendaBackgroundTests):
    pass


class LinkedContactChecks(LinkedConversation, contacts.ProactiveContactTests):
    pass


class RoutedContactTests(LinkedConversation, ContactFixture, unittest.IsolatedAsyncioTestCase):
    """A proactive contact of a linked conversation leaves through the preferred member only."""

    def setUp(self):
        super().setUp()
        self.preferred = self.other_window_context().scope

    async def test_a_contact_is_delivered_to_the_preferred_member_and_counted_once_for_the_conversation(self):
        app = await self.enabled()
        task = (await self.contact(app)).unwrap().task
        row = await self.delivery_state(app, task.task_id, "delivered")
        self.assertEqual([envelope.context.scope for envelope in self.sent], [self.preferred])
        self.assertEqual(row["origin_key"], self.contact_context().key)
        route = app.outbound.route(row["delivery_id"])
        self.assertEqual((route["scope"], route["link_id"], route["link_revision"]),
                         (self.preferred.document(), "link", 1))
        usage = app.proactive.budget.usage_summary(self.contact_context(), self.preferences(app), self.clock.now())
        self.assertEqual(usage["deliveries"]["session"]["delivered"], 1)

    async def test_a_preferred_member_without_proactive_consent_gets_nothing_and_no_other_member_is_tried(self):
        app = await self.enabled()
        policy, identity = app.tasks.policy, app.tasks.identity
        policy.set_tier(self.important.subject, AuthorizationTier.STANDARD,
                        expected_revision=policy.revision, observed_at=self.clock.now())
        for number, capability in enumerate(sorted(REQUIRED_CAPABILITIES | {"proactive.contact"})):
            identity.issue_grant(grant_id=f"own-{number}", subject=self.important.subject, scope=SCOPE,
                capability=capability, tier=AuthorizationTier.STANDARD, valid_from=self.clock.now(),
                expected_revision=identity.revision)
        for grant in identity.snapshot().grants:
            if grant.scope == self.preferred and grant.capability == "proactive.contact":
                identity.revoke_grant(grant.grant_id, revoked_at=self.clock.now(), expected_revision=identity.revision)
        task = (await self.contact(app)).unwrap().task
        row = await self.delivery_state(app, task.task_id, "cancelled")
        self.assertEqual(row["reason"], "authorization_revoked")
        self.assertEqual(self.sent, [])

    async def at_the_platform_call(self, app, change):
        """Edit the link after the route was resolved; say what the sender's last check answers."""
        refused = []

        async def edited(envelope):
            change()
            self.link(app.runtime)
            try:
                envelope.before_dispatch()
            except TaskError as error:
                refused.append(error.code)
                return self.receipt(envelope, "not_sent")
            return self.receipt(envelope)
        self.send_behavior = edited
        task = (await self.contact(app)).unwrap().task
        await self.until(lambda: (app.proactive._delivery_row(task.task_id) or {"state": ""})["state"]
                         in ("delivered", "failed"))
        return app.proactive._delivery_row(task.task_id)["state"], refused

    async def test_a_new_link_revision_with_the_same_preferred_member_does_not_stop_a_contact(self):
        app = await self.enabled()
        self.assertEqual(await self.at_the_platform_call(app, lambda: setattr(self, "link_revision", 2)),
                         ("delivered", []))

    async def test_another_preferred_member_chosen_before_the_platform_call_stops_the_contact(self):
        app = await self.enabled()
        self.assertEqual(await self.at_the_platform_call(app, lambda: setattr(self, "preferred", None)),
                         ("failed", ["delivery_route_changed"]))
