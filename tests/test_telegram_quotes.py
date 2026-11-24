"""Synthetic Telegram updates passed through AstrBot's installed converter."""

import base64
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_telegram_platform import OWNER, TelegramHostFixture
from telegram import PhotoSize, Update
from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
from astrbot.core.message.components import Image, Reply
from companion_v2.execution.models import require
from astrbot_host.media import load_native_image


class TelegramQuoteTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.platform.client.id = 424242
        self.platform.client.username = "synthetic_bot"
        self.types = replace(self.types, image=Image, image_loader=load_native_image,
                                  reply=Reply, quote_loader=AsyncMock(side_effect=AssertionError("Telegram must not look up the quoted message")))
        config = self.controller.config
        config["multi_host"]["targets"].append(dict(config["multi_host"]["targets"][0],
            target_id="tg-linked", participant_id=str(OWNER + 1)))
        self.window_personas[f"tg-main:FriendMessage:{OWNER + 1}"] = "original"
        self.controller = self.make_controller(config)
        await self.start_host()
        self.bridge = self.controller.session.bridge

    def message(self, number, sender=OWNER, **fields):
        return {"message_id": number, "date": 1700000000, "chat": {"id": OWNER, "type": "private"},
                "from": {"id": sender, "is_bot": sender == 424242, "first_name": "Synthetic"}, **fields}

    async def event(self, number=12, **fields):
        raw = Update.de_json({"update_id": 12, "message": self.message(number, **fields)}, None)
        message = await self.platform.convert_message(raw, NS(bot=self.platform.client))
        event = TelegramPlatformEvent(message.message_str, message, self.platform.meta(), str(OWNER), self.platform.client)
        event.created_at = self.clock.now().timestamp()
        return event

    def observe(self, original):
        session = self.controller.session
        context = session.authority.select(self.controller.settings.target("tg-owner"), original.route.scope, original.role)
        reference = session.core_reply_observations.owner.capture_input(context, original, turn_id="test-" + original.event_id)
        access = session.application.memory.writer.access
        evidence = access.capture(context, reference)
        return access.evidence(context, evidence), (evidence.speaker_id, evidence.source_kind)

    def quoted_evidence(self, result):
        session = self.controller.session
        context = session.authority.select(self.controller.settings.target("tg-owner"), result.route.scope, result.role)
        row = self.controller.runtime.read_one("""SELECT source_json FROM conversation_native_observations
            WHERE event_id=? AND json_extract(source_json, '$.owner')='conversation.native_reference'""",
            (result.event_id,))
        from companion_v2.memory.codec import source
        access = session.application.memory.writer.access
        quoted = access.capture(context, source(json.loads(row["source_json"])))
        self.assertEqual((quoted.speaker_id, quoted.source_kind), (None, "observation"))
        return access.evidence(context, quoted)

    def prompt(self, event, original, text):
        from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA, inject_core_reply_prompt

        event.set_extra(CORE_REPLY_CONTEXT_EXTRA, {"prepared": {"native_input": original}})
        request = NS(prompt=text, system_prompt="")
        inject_core_reply_prompt(event, request)
        inject_core_reply_prompt(event, request)
        return request.prompt + request.system_prompt

    async def test_private_quotes_preserve_original_source_and_selected_text(self):
        from astrbot.core.utils.quoted_message.chain_parser import ReplyChainParser

        for index, (sender, selection) in enumerate(((OWNER, None), (424242, None), (OWNER, {"text": "片段", "position": 2}))):
            fields = {"quote": selection} if selection else {}
            event = await self.event(number=30 + index, text="说说看", reply_to_message=self.message(10, sender, text="😀片段原文"), **fields)
            original = selection["text"] if selection else "😀片段原文"
            for capture in (self.bridge.capture, self.bridge.capture_core_reply):
                with self.subTest(sender=sender, capture=capture.__name__, selection=selection):
                    result = require(await capture(event))
                    quote, = result.quotes
                    self.assertEqual((quote.message_id, quote.sender_id, quote.sent_at, quote.text),
                                     ("10", str(sender), 1700000000, original))
                    self.assertNotIn(original, result.text)
            human_text, (_, kind) = self.observe(result)
            self.assertEqual((human_text, kind), ("说说看", "human_message"))
            record = self.quoted_evidence(result)
            self.assertIn(original, record)
            self.assertIn(str(sender), record)
            if selection:
                self.assertNotIn("原文", record)
            parser = ReplyChainParser()
            sdk_text = parser.extract_text_from_reply_component(parser.find_first_reply_component(event))
            self.assertEqual(self.prompt(event, result, sdk_text + event.message_str).count(original), 1)

    async def test_missing_and_external_originals_are_unavailable(self):
        for fields in ({"reply_to_message": self.message(10, date=0, **{"from": None})},
                       {"quote": {"text": "不可见", "position": 0}},
                       {"external_reply": {"origin": {"type": "hidden_user", "sender_user_name": "X",
                                                       "date": 1700000000}}},
                       {"reply_to_message": self.message(10, chat={"type": "private", "id": OWNER + 1},
                                                         text="不可见")}):
            event = await self.event(text="继续", **fields)
            for capture in (self.bridge.capture, self.bridge.capture_core_reply):
                result = require(await capture(event))
                self.assertIn("引用消息不可用", result.text)
                self.assertIn("继续", result.text)
                self.assertNotIn("不可见", result.text)
                self.assertFalse(result.quotes)

    async def test_each_forward_origin_is_preserved_in_the_observation(self):
        from astrbot_host.core_reply_forwards import private_debounce_input
        from companion_v2.relationships.account_link_context import linked_context
        from companion_v2.relationships.account_link_history import history
        from companion_v2.relationships.account_links import PrivateAccountLinks
        session = self.controller.session
        app = session.application
        owner, _ = await session.synchronize(self.controller.settings.target("tg-owner"))
        peer, _ = await session.synchronize(self.controller.settings.target("tg-linked"))
        PrivateAccountLinks(app.runtime).configure(owner, (owner, peer), persona_id="original",
            expected_revision=0, share_context=True, operation_key="link", operator_id="test", at=self.clock.now())
        self.clock.advance(timedelta(seconds=1))
        origins = (
            ({"type": "user", "sender_user": self.message(3, 42)["from"]}, "Synthetic (42)"),
            ({"type": "hidden_user", "sender_user_name": "Hidden"}, "Hidden"),
            ({"type": "chat", "sender_chat": {"type": "supergroup", "id": -100, "title": "Group"}}, "Group (-100)"),
            ({"type": "channel", "message_id": 41, "author_signature": "Editor",
              "chat": {"type": "channel", "id": -200, "title": "Channel"}}, "Channel (-200)，原消息 41，署名 Editor"),
        )
        for index, (origin, name) in enumerate(origins):
            event = await self.event(number=40 + index, text="别人的话", forward_origin=dict(origin, date=1700000000))
            self.assertIsNone(private_debounce_input(event))
            full = require(await self.bridge.capture(event))
            self.assertNotIn("别人的话", full.text)
            node, = full.forwards[0].nodes
            self.assertEqual((node.text, node.sender_name, node.sent_at), ("别人的话", name, 1700000000))
            core = require(await self.bridge.capture_core_reply(event))
            record, attribution = self.observe(core)
            self.assertEqual(record, f"[转发消息，来源：{name}]\n别人的话")
            self.assertEqual(attribution, (None, "observation"))
            self.assertEqual(self.prompt(event, core, "别人的话"), record)
            page = history(app, peer, at=self.clock.now())["records"]
            shared = json.loads(linked_context(app, peer, at=self.clock.now()).splitlines()[-1])
            for rows in (page, shared):
                self.assertEqual([r["speaker"] for r in rows if r["text"] == record], ["observation"])

    async def test_dialogue_and_journal_only_attribute_original_words_to_the_owner(self):
        from companion_v2.conversation.native_history import dialogue
        from companion_v2.execution.models import stamp
        from companion_v2.foundation.refs import RoleOwnerRef
        from companion_v2.memory.episodes import recent_dialogue_lines
        from companion_v2.role_work.journal.models import JournalContext, JournalKey, JournalKind

        session = self.controller.session
        app = session.application
        reply = await self.event(number=91, text="我喜欢绿色", reply_to_message=self.message(89, 424242, text="蓝色"))
        forward = await self.event(number=92, text="红色", forward_origin={
            "type": "hidden_user", "sender_user_name": "Hidden", "date": 1700000000})
        for event in (reply, forward):
            captured = require(await self.bridge.capture_core_reply(event))
            self.observe(captured)
        context = session.authority.select(self.controller.settings.target("tg-owner"), captured.route.scope, captured.role)
        now = self.clock.now()
        manifest = {"start": stamp(now - timedelta(seconds=1)), "end": stamp(now + timedelta(seconds=1))}
        for lines in (recent_dialogue_lines(app.runtime, app.memory.writer.access, context, manifest, limit=1),
                      dialogue(app.messages, context, now, limit=1)):
            self.assertEqual([(row["speaker"], row["text"]) for row in lines], [("user", "我喜欢绿色")])
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        key = JournalKey.at(owner, JournalKind.DIARY, now, timezone="Asia/Shanghai")
        page = app.journal_conversation_material_producer.for_day(context, JournalContext(key, context.scope, "Asia/Shanghai"))
        self.assertEqual([item.text for item in page.materials], ["对方当时表达：我喜欢绿色"])

    async def test_captioned_photos_keep_the_original_sender_and_image(self):
        from astrbot_host.core_reply_media import frozen_image_locators, prepare_native_input_images
        from tests.test_conversation_images import image_bytes
        source = image_bytes("green")
        photo = [{"file_id": "file", "file_unique_id": "unique", "width": 2, "height": 2}]
        for sender in (OWNER, 424242):
            with patch.object(PhotoSize, "get_file", new=AsyncMock(return_value=NS(
                    file_path="base64://" + base64.b64encode(source).decode()))):
                event = await self.event(number=20 if sender == OWNER else 21, text="修改", reply_to_message=self.message(10, sender, photo=photo, caption="绿"))
            quote, = require(await self.bridge.capture(event)).quotes
            self.assertEqual((quote.text, quote.sender_id), ("\n[图片 1]\n绿", str(sender)))
            self.assertEqual(len(quote.images), 1)
            with patch.object(PhotoSize, "get_file", new=AsyncMock(return_value=NS(file_path="unused"))):
                selection = await self.event(number=22, text="引用", quote={"text": "绿", "position": 0},
                    reply_to_message=self.message(10, sender, photo=photo, caption="绿色"))
            partial, = require(await self.bridge.capture(selection)).quotes
            self.assertEqual((partial.text, partial.images, frozen_image_locators(selection)), ("绿", (), ()))
            original = require(await self.bridge.capture_core_reply(event))
            session = self.controller.session
            target = self.controller.settings.target("tg-owner")
            context = session.authority.select(target, original.route.scope, original.role)
            tool = NS(application=session.application, prepared={"context": context, "native_input": original},
                      image_locators=frozen_image_locators(event), authorize=AsyncMock(), turn_id=f"photo-{sender}")
            await prepare_native_input_images(tool, None)
            from companion_v2.conversation.native_tool_sources import native_input_images
            saved = native_input_images(session.application.messages, context, tool.turn_id)
            payload = session.application.messages._read_source(saved.source, context, "text", self.clock.now()).payload
            self.assertEqual(json.loads(payload)["image_sources"][0]["original_sender"], str(sender))

    async def test_selection_of_a_previously_admitted_message_can_be_replayed(self):
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        body = dict(text="😀链接片段原文", entities=[dict(type="text_link", offset=2, length=2,
                                                    url="https://example.org/synthetic")])
        admitted = await self.event(number=10, **body)
        self.assertIn(body["entities"][0]["url"], admitted.message_str)
        require(await session.receive(admitted, target))
        whole = await self.event(number=13, text="完整引用", reply_to_message=self.message(10, **body))
        require(await session.receive(whole, target))
        event = await self.event(number=11, text="再说说", quote={"text": "片段", "position": 4},
                                 reply_to_message=self.message(10, **body))
        require(await session.receive(event, target))
        require(await session.receive(event, target))
        changed = dict(body, entities=[dict(body["entities"][0], url="https://example.org/changed")])
        for number, original, selection in (
            (14, changed, {"text": "片段", "position": 4}),
            (15, body, {"text": "片段", "position": 3}),
        ):
            invalid = await self.event(number=number, text="校验", quote=selection,
                                       reply_to_message=self.message(10, **original))
            outcome = await session.receive(invalid, target)
            self.assertEqual(outcome.failure.code, "quote_original_changed")

    async def test_replies_to_admitted_forwards_and_replies_verify_the_embedded_body(self):
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        origins = (
            {"type": "hidden_user", "sender_user_name": "Hidden"},
            {"type": "user", "sender_user": self.message(1, 42)["from"]},
            {"type": "chat", "sender_chat": {"id": -111, "type": "supergroup", "title": "Group"}},
            {"type": "channel", "chat": {"id": -222, "type": "channel", "title": "News"}, "message_id": 9},
        )
        for index, origin in enumerate(origins):
            number = 100 + 10 * index
            forwarded = dict(text="😀原文", forward_origin=dict(origin, date=1699990000))
            require(await session.receive(await self.event(number, **forwarded), target))
            quoted = self.message(number, **forwarded)
            for seq, selection in enumerate(({}, {"quote": {"text": "原", "position": 2}}), 1):
                event = await self.event(number + seq, text="讨论", reply_to_message=quoted, **selection)
                full = require(await self.bridge.capture_core_reply(event))
                quote, = full.quotes
                self.assertEqual(quote.sender_id, str(OWNER))
                self.assertEqual(quote.forward_origin.kind, origin["type"])
                self.observe(full)
                evidence = self.quoted_evidence(full)
                self.assertIn(quote.forward_origin.attribution(str(OWNER)), evidence)
                if selection:
                    self.assertNotIn("原文", evidence)
                prompt = self.prompt(event, full, quote.text + "讨论")
                self.assertIn(quote.forward_origin.attribution(str(OWNER)), prompt)
                self.assertEqual(prompt.count(quote.text), 1)
                require(await session.receive(event, target))
            for seq, changes in enumerate((dict(text="其他"), dict(forward_origin=None),
                                          dict(forward_origin=dict(origin, date=1699990001))), 3):
                changed = await self.event(number + seq, text="?", reply_to_message=dict(quoted, **changes))
                self.assertEqual((await session.receive(changed, target)).failure.code, "quote_original_changed")
            nested = await self.event(number + 6, text="继续", reply_to_message=self.message(number + 1, text="讨论"))
            require(await session.receive(nested, target))
            changed = await self.event(number + 7, text="继续", reply_to_message=self.message(number + 1, text="错误"))
            self.assertEqual((await session.receive(changed, target)).failure.code, "quote_original_changed")

    async def test_admitted_static_sticker_keeps_converted_image_and_emoji(self):
        from telegram import Sticker
        from astrbot_host.core_reply_media import frozen_image_locators
        from tests.test_conversation_images import image_bytes

        sticker = dict(file_id="file", file_unique_id="unique", type="regular", width=2, height=2,
                       emoji="🍀", is_animated=False, is_video=False)
        url = "base64://" + base64.b64encode(image_bytes("green")).decode()
        target = self.controller.settings.target("tg-owner")
        with patch.object(Sticker, "get_file", new=AsyncMock(return_value=NS(file_path=url))):
            original = await self.event(10, sticker=sticker)
            quoted = await self.event(11, text="看看", reply_to_message=self.message(10, sticker=sticker))
        require(await self.controller.session.receive(original, target))
        capture = require(await self.bridge.capture(quoted))
        quote, = capture.quotes
        self.assertEqual(quote.text, "\n[图片 1]\nSticker: 🍀")
        self.assertEqual(len(quote.images), 1)
        self.assertEqual(len(frozen_image_locators(quoted)[0].images), 1)
        require(await self.controller.session.receive(quoted, target))
