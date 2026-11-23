"""One exact Telegram account can be recorded as the person of an earlier channel."""

from dataclasses import replace
import unittest

from companion_v2.execution.models import TaskError, digest
from tests.test_telegram_platform import OWNER, TelegramHostFixture

EARLIER = "onebot-user:200"


class CarriedPersonTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    def record(self, **changes):
        authority, target = self.controller.authority, self.controller.settings.target("tg-owner")
        authority.write("host.multi.person:" + digest(authority.principal(target)),
                        {"platform_name": "telegram", "subject_id": EARLIER, **changes})
        return authority, target

    async def test_a_recorded_account_is_admitted_as_the_earlier_person_before_and_after_its_channel_loads(self):
        await self.controller._open_async()
        authority, target = self.record()
        self.assertEqual(authority.subject(replace(target, participant_id="9")).subject_id, "telegram-user:9")
        self.assertEqual(authority.subject(replace(target, bot_id="bot-b")).subject_id, f"telegram-user:{OWNER}")
        self.assertEqual(authority.subject(self.controller.settings.target("tg-owner")).subject_id, EARLIER)
        del self.native.platforms["tg-main"]
        plugin = self.started_plugin()
        self.assertTrue(await plugin._try_start_host())
        self.assertEqual(self.bound_person(), EARLIER)
        await self.connect_late(plugin)
        self.assertEqual(await self.admitted(plugin, 1), EARLIER)

    async def test_a_record_for_another_kind_of_channel_is_ignored(self):
        await self.controller._open_async()
        authority, target = self.record(platform_name="weixin_oc")
        self.assertEqual(authority.subject(target).subject_id, f"telegram-user:{OWNER}")

    async def test_an_account_already_bound_to_someone_else_is_refused(self):
        await self.start_host()
        authority, target = self.record()
        with self.assertRaises(TaskError) as refused:
            authority.actor(target)
        self.assertEqual(refused.exception.code, "host_identity_changed")


if __name__ == "__main__":
    unittest.main()
