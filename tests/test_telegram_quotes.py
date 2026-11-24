"""Synthetic Telegram updates passed through AstrBot's installed converter."""

import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_telegram_platform import OWNER, TelegramHostFixture
from telegram import Document, PhotoSize, Update
from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
from astrbot.core.message.components import File, Image, Reply
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

    async def test_forward_burst_and_comment_produce_one_attributed_turn(self):
        from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA

        plugin = self.started_plugin()
        events = [await self.event(number=80 + i, text=body,
            forward_origin={"type": "hidden_user", "sender_user_name": author, "date": 1700000000})
            for i, (author, body) in enumerate((("Ada", "first-paper"), ("Bo", "second-paper"),
                                                ("Chen", "third-paper")))]
        events.append(await self.event(number=83, text="my-comment"))
        earlier = await self.event(number=79, text="previous turn")
        active = plugin._private_reply_followups().start(
            "tg-owner", earlier, "79", ((earlier, "previous turn", earlier.created_at),))
        with patch.object(self.controller, "prepare_core_reply",
                          wraps=self.controller.prepare_core_reply) as prepare:
            await asyncio.gather(*(plugin.receive_companion_message(item) for item in events))
        self.assertEqual([entry[1] for entry in active.entries], ["previous turn"])
        self.assertEqual(prepare.await_count, 1)
        winner, = [item for item in events if item.get_extra(CORE_REPLY_CONTEXT_EXTRA)]
        original = winner.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]["native_input"]
        self.assertEqual([item.event_id for item in original.telegram_burst], ["80", "81", "82", "83"])
        for author, body in (("Ada", "first-paper"), ("Bo", "second-paper"), ("Chen", "third-paper")):
            self.assertIn(f"来源：{author}]\n{body}", winner.message_str)
            self.assertEqual(winner.message_str.count(body), 1)
        self.assertEqual(winner.message_str.count("my-comment"), 1)
        from astrbot.core.astr_main_agent import collect_initial_request
        with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            request, _ = await collect_initial_request(winner, NS(), NS(provider_wake_prefix="", provider_settings={}))
        payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
        for body in ("first-paper", "second-paper", "third-paper", "my-comment"):
            self.assertEqual(payload.count(body), 1)
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        context = session.authority.select(target, original.route.scope, original.role)
        session.core_reply_observations.owner.capture_input(
            context, original, turn_id=winner.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]["turn_id"])
        rows = self.controller.runtime.read_all(
            "SELECT event_id, json_extract(source_json, '$.owner') AS owner "
            "FROM conversation_native_observations WHERE event_id IN ('80','81','82','83') ORDER BY event_id", ())
        self.assertEqual([row["owner"] for row in rows],
                         ["conversation.native_reference"] * 3 + ["conversation.native_input"])

    async def test_forwarded_command_stays_reference_text(self):
        from astrbot_host.binding import message_text
        from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA

        command = "陪伴 全部重置"
        forward = await self.event(number=95, text=command, forward_origin={
            "type": "hidden_user", "sender_user_name": "Ada", "date": 1700000000})
        direct = await self.event(number=96, text=command)
        self.assertEqual(message_text(direct), command)
        self.assertEqual(message_text(forward), "")

        plugin = self.started_plugin()
        receipts = AsyncMock()
        with patch.object(self.controller, "process_private_relay_receipts", receipts, create=True):
            await plugin.receive_companion_message(forward)
        receipts.assert_not_awaited()
        original = forward.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]["native_input"]
        self.assertEqual(original.forward_origin, "Ada")
        self.assertIn(command, original.text)

    async def test_forward_closes_earlier_followup_before_command_processing(self):
        plugin = self.started_plugin()
        earlier = await self.event(number=86, text="earlier turn")
        first = await self.event(number=87, text="first comment")
        later = await self.event(number=89, text="later comment")
        forward = await self.event(number=88, text="excerpt", forward_origin={
            "type": "hidden_user", "sender_user_name": "Ada", "date": 1700000000})
        manager = plugin._private_reply_followups()
        manager.start("tg-owner", earlier, "86", ((earlier, earlier.message_str, earlier.created_at),))
        self.assertIsNotNone(manager.fold("tg-owner", first, first.message_str))
        reserve = plugin._private_debounce_manager().reserve

        def check_boundary(*args):
            self.assertIsNone(manager.fold("tg-owner", later, later.message_str))
            pending, _ = manager.for_request(earlier)
            self.assertEqual([entry[1] for entry in pending], ["first comment"])
            return reserve(*args)

        with patch.object(plugin._private_debounce_manager(), "reserve", side_effect=check_boundary) as called:
            await plugin.receive_companion_message(forward)
        called.assert_called_once()

    def test_many_forwards_share_the_prompt_budget(self):
        from astrbot_host.telegram_forward_burst import _bounded_sections
        from companion_v2.conversation.private_debounce import MAX_MERGED_CHARS

        originals = [NS(forward_origin=f"{i:03d}-" + "x" * 55, text="paper" * 800) for i in range(100)]
        originals.append(NS(forward_origin="", text="my comment"))
        sections = list(_bounded_sections(originals))
        self.assertEqual([i for i, _ in sections], list(range(101)))
        self.assertLessEqual(len("\n\n".join(section for _, section in sections)), MAX_MERGED_CHARS)
        for i, section in sections[:-1]:
            self.assertIn(f"{i:03d}-", section)
        self.assertIn("my comment", sections[-1][1])

    async def test_long_forward_burst_keeps_each_source_and_owner_comment(self):
        from astrbot_host.telegram_forward_burst import merge_forward_burst
        from companion_v2.conversation.private_debounce import MAX_MERGED_CHARS

        events = [await self.event(number=100+i, text=letter * 4096,
            forward_origin={"type": "hidden_user", "sender_user_name": name, "date": 1700000000})
            for i, (letter, name) in enumerate(zip("ABC", ("Ada", "Bo", "Chen")))]
        comment = await self.event(number=103, text="compare their last points")
        events.append(comment)
        prepare = self.controller.prepare_core_reply_context
        target = self.controller.settings.target("tg-owner")
        plan = (await prepare(comment, target)).unwrap()
        merged = await merge_forward_burst(comment, target, plan, [(e, "", None) for e in events], prepare)
        self.assertLessEqual(len(merged.captured.text), MAX_MERGED_CHARS)
        for letter, name in zip("ABC", ("Ada", "Bo", "Chen")):
            self.assertIn(f"[转发消息，来源：{name}]\n{letter * 5}", comment.message_str)
        self.assertTrue(comment.message_str.endswith("[发起人的留言]\ncompare their last points"))
        self.assertEqual(len(merged.captured.telegram_burst), 4)

    async def test_forward_burst_ending_in_photo_keeps_media_and_comment(self):
        from astrbot_host.telegram_forward_burst import merge_forward_burst
        from tests.test_conversation_images import image_bytes

        comment = await self.event(number=90, text="look at this")
        photo = [{"file_id": "file", "file_unique_id": "unique", "width": 2, "height": 2}]
        with patch.object(PhotoSize, "get_file", new=AsyncMock(return_value=NS(
                file_path="base64://" + base64.b64encode(image_bytes("green")).decode()))):
            forwarded = await self.event(number=91, photo=photo, caption="green picture",
                forward_origin={"type": "hidden_user", "sender_user_name": "Ada", "date": 1700000000})
        target = self.controller.settings.target("tg-owner")
        prepare = self.controller.prepare_core_reply_context
        plan = (await prepare(forwarded, target)).unwrap()
        merged = await merge_forward_burst(forwarded, target, plan,
            ((comment, "look at this", None), (forwarded, "", None)), prepare)
        self.assertEqual([item.forward_origin for item in merged.captured.telegram_burst], ["", "Ada"])
        self.assertEqual(sum(isinstance(part, Image) for part in forwarded.message_obj.message), 1)
        for word in ("look at this", "green picture", "来源：Ada"):
            self.assertEqual(forwarded.message_str.count(word), 1)

    async def test_numeric_instance_id_is_not_the_author_of_a_quote(self):
        from astrbot_host.core_reply_decision import core_decision_facts

        for author in (OWNER, self.platform.client.id):
            event = await self.event(text="答复", reply_to_message=self.message(10, author, text="原文"))
            native = require(await self.bridge.capture_core_reply(event))
            prepared = {"native_input": native, "context": NS(scope=NS(account_id=str(OWNER))),
                        "channel": "private"}
            facts = core_decision_facts(event, prepared, None, at=self.clock.now())
            self.assertFalse(facts["is_bot_message"])
            self.assertEqual(facts["quoted_bot_message"], author == self.platform.client.id)
            self.assertEqual(facts["bot_account_id"], str(self.platform.client.id))

            from companion_v2.conversation.generation import LocalReplyGenerator
            generator = self.controller.session.application.generator
            self.assertEqual(generator.resolve_bot_sender(native.route.scope), self.platform.client.id)
            request = NS(context=NS(materials=()), images=(), audios=(),
                         event=NS(scope=NS(kind=native.route.scope.kind, account_id=str(OWNER)),
                                  principal=native.principal, quotes=native.quotes))
            local = LocalReplyGenerator._laya_facts(request, lambda scope: generator.resolve_bot_sender(native.route.scope))
            self.assertFalse(local.get("is_bot_message", False))
            self.assertEqual(local.get("quoted_bot_message", False), author == self.platform.client.id)

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

    async def test_external_bot_quote_is_not_an_admitted_bot_reference(self):
        from astrbot_host.core_reply_decision import core_decision_facts

        foreign = self.message(10, self.platform.client.id, chat={"type": "private", "id": OWNER + 1}, text="别处")
        event = await self.event(text="继续", reply_to_message=foreign)
        native = require(await self.bridge.capture_core_reply(event))
        self.assertFalse(native.quotes)
        facts = core_decision_facts(event, {"channel": "private", "native_input": native}, None, at=self.clock.now())
        self.assertFalse(facts["quoted_bot_message"])

        from astrbot.core.astr_main_agent import _process_quote_message, collect_initial_request

        with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
            await _process_quote_message(event, request, "", NS())
        payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
        self.assertIn("继续", payload)
        self.assertNotIn("别处", payload)

    async def test_forward_label_cannot_change_the_kind_of_an_admitted_origin(self):
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        hidden = {"type": "hidden_user", "sender_user_name": "Group (-111)", "date": 1699990000}
        chat = {"type": "chat", "sender_chat": {"id": -111, "type": "supergroup", "title": "Group"},
                "date": hidden["date"]}
        with patch.object(session.application, "worker_limit", 0):
            require(await session.receive(await self.event(10, text="相同正文", forward_origin=hidden), target))
            forged = self.message(10, text="相同正文", forward_origin=chat)
            result = await session.receive(await self.event(11, text="继续", reply_to_message=forged), target)
            self.assertFalse(result.is_success)
            self.assertEqual(result.failure.code, "quote_original_changed")

    async def test_forward_chat_proof_uses_the_native_chat_id(self):
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        first = dict(type="chat", date=1699990000, sender_chat=dict(id=-1001, type="supergroup", title="Club"),
                     author_signature="A (-1002)")
        other = dict(type="chat", date=first["date"], sender_chat=dict(id=-1002, type="supergroup",
                     title="Club (-1001)，署名 A"))
        with patch.object(session.application, "worker_limit", 0):
            require(await session.receive(await self.event(10, text="原文", forward_origin=first), target))
            for number, origin in ((11, first), (12, other)):
                embedded = self.message(10, text="原文", forward_origin=origin)
                result = await session.receive(await self.event(number, text="继续", reply_to_message=embedded), target)
                if number == 11:
                    require(result)
                else:
                    self.assertEqual(result.failure.code, "quote_original_changed")

    async def test_quote_verifies_original_forward_label_after_redaction(self):
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        hidden = {"type": "hidden_user", "sender_user_name": "token=demo", "date": 1699990000}
        with patch.object(session.application, "worker_limit", 0):
            event = await self.event(10, text="原文", forward_origin=hidden)
            captured = require(await self.bridge.capture(event))
            self.assertIn("token=[REDACTED]", captured.text)
            self.assertNotIn("token=demo", captured.text)
            require(await session.receive(event, target))
            for number, name in ((11, "token=demo"), (12, "token=changed")):
                embedded = self.message(10, text="原文", forward_origin={**hidden, "sender_user_name": name})
                result = await session.receive(await self.event(number, text="继续", reply_to_message=embedded), target)
                if number == 11:
                    require(result)
                else:
                    self.assertEqual(result.failure.code, "quote_original_changed")

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

    async def test_unavailable_quote_keeps_the_document_and_its_caption(self):
        self.bridge.file_type = File
        self.bridge.file_settings = lambda *_: {"file_understanding_enabled": False}
        self.bridge.file_loader = AsyncMock()
        original = self.message(10, chat={"type": "private", "id": OWNER + 1}, text="不可见")
        with patch.object(Document, "get_file", new=AsyncMock(return_value=NS(file_path="https://example.org/demo"))):
            event = await self.event(reply_to_message=original, caption="这是说明",
                                     document={"file_id": "doc", "file_unique_id": "d", "file_name": "demo.txt"})
        result = require(await self.bridge.capture(event))
        self.assertIn("引用消息不可用", result.text)
        self.assertIn("demo.txt", result.text)
        self.assertIn("这是说明", result.text)
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
            self.assertEqual(private_debounce_input(event), ("", True))
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

    async def test_oversized_quoted_photo_keeps_original_attribution_and_caption(self):
        from astrbot_host.media import load_native_image
        from tests.test_conversation_images import image_bytes

        self.bridge.image_loader = load_native_image
        source = image_bytes(size=(8193, 1))
        photo = [{"file_id": "file", "file_unique_id": "unique", "width": 8193, "height": 1}]
        with patch.object(PhotoSize, "get_file", new=AsyncMock(return_value=NS(
                file_path="base64://" + base64.b64encode(source).decode()))):
            event = await self.event(text="继续", reply_to_message=self.message(10, 424242, photo=photo, caption="原图"))
        result = require(await self.bridge.capture(event))
        quote, = result.quotes
        self.assertEqual((quote.message_id, quote.sender_id, quote.sent_at), ("10", "424242", 1700000000))
        self.assertTrue(quote.companion_sender)
        self.assertIn("原图", quote.text)
        self.assertEqual(quote.images, ())
        self.assertIn("继续", result.text)
        self.assertNotIn("原图", result.text)

    async def test_admitted_mentions_keep_their_positions_across_links_and_partial_quotes(self):
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        body = dict(text="😀A @Synthetic_Bot B ok", entities=[
            dict(type="text_link", offset=2, length=1, url="https://example.org/a"),
            dict(type="mention", offset=4, length=14),
            dict(type="text_link", offset=19, length=1, url="https://example.org/b"),
        ])
        event = await self.event(10, **body)
        expected = "😀A (https://example.org/a) @Synthetic_Bot B (https://example.org/b) ok"
        self.assertNotIn("@Synthetic_Bot", event.message_str)
        require(await session.receive(event, target))
        for number, selection in ((11, {}), (12, {"quote": {"text": "@Synthetic_Bot B", "position": 4}})):
            reply = await self.event(number, text="讨论", reply_to_message=self.message(10, **body), **selection)
            require(await session.receive(reply, target))
        for capture in (self.bridge.capture, self.bridge.capture_core_reply):
            self.assertEqual(require(await capture(event)).text, expected)

    async def test_private_mention_restoration_preserves_attribution_and_transformed_text(self):
        body = dict(text="@synthetic_bot", entities=[dict(type="mention", offset=0, length=14)])
        forwarded = await self.event(21, **body, forward_origin={
            "type": "hidden_user", "sender_user_name": "Hidden", "date": 1700000000})
        full = require(await self.bridge.capture(forwarded))
        self.assertNotIn(body["text"], full.text)
        self.assertEqual(full.forwards[0].nodes[0].text, body["text"])
        core = require(await self.bridge.capture_core_reply(forwarded))
        self.assertEqual(self.observe(core), ("[转发消息，来源：Hidden]\n@synthetic_bot", (None, "observation")))

        event = await self.event(22, **dict(body, text="@synthetic_bot private"))
        event.message_obj.message[-1].text = "edited"
        for capture in (self.bridge.capture, self.bridge.capture_core_reply):
            self.assertEqual(require(await capture(event)).text, "edited")

    async def test_old_body_without_a_mention_cannot_prove_its_original_position(self):
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        # This is also the only text available from pre-fix admissions.
        require(await session.receive(await self.event(10, text="hi  ok"), target))
        original = self.message(10, text="hi @synthetic_bot ok", entities=[
            dict(type="mention", offset=3, length=14)])
        for number, selection in ((11, {}), (12, {"quote": {"text": "@synthetic_bot", "position": 3}})):
            reply = await self.event(number, text="讨论", reply_to_message=original, **selection)
            self.assertEqual((await session.receive(reply, target)).failure.code, "quote_original_changed")

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
        from astrbot.core.utils.quoted_message.chain_parser import ReplyChainParser

        parser = ReplyChainParser()
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
                sdk_quote = parser.extract_text_from_reply_component(parser.find_first_reply_component(event))
                prompt = self.prompt(event, full, sdk_quote + event.message_str)
                self.assertEqual(prompt, f"[引用消息 {number}，{quote.forward_origin.attribution(str(OWNER))}]\n"
                                         + sdk_quote + event.message_str)
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
        self.assertEqual(quote.text, "\n[图片 1]\n[贴纸 🍀]")
        self.assertEqual(len(quote.images), 1)
        self.assertEqual(len(frozen_image_locators(quoted)[0].images), 1)
        require(await self.controller.session.receive(quoted, target))

    async def test_voice_reply_preserves_caption_and_quote_through_provider_and_replay(self):
        from pathlib import Path
        from telegram import Voice
        from astrbot.core.astr_main_agent import MainAgentBuildConfig, _process_quote_message, collect_initial_request
        from astrbot.core.message.components import Record
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
        from astrbot_host.audio.loader import load_native_audio
        from tests.audio_support import audio_bytes

        self.bridge.audio_type, self.bridge.audio_loader = Record, load_native_audio
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        caption = "这是语音说明"
        quote_text = "先前的答复"
        original = self.message(9, 424242, text=quote_text)
        voice = {"file_id": "voice", "file_unique_id": "v1", "duration": 1}

        async def download(url, *, path):
            Path(path).write_bytes(audio_bytes())

        Path(get_astrbot_temp_path()).mkdir(parents=True, exist_ok=True)
        with patch.object(Voice, "get_file", AsyncMock(return_value=NS(file_path="https://example.org/voice"))), \
                patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", download), \
                patch.object(session.application, "worker_limit", 0):
            converted = await self.event(90, text="继续", reply_to_message=original)
            reply = next(part for part in converted.message_obj.message if isinstance(part, Reply))
            for number, with_component in ((100, False), (110, True)):
                event = await self.event(number, voice=voice, caption=caption, reply_to_message=original)
                components = event.message_obj.message
                components[:] = [part for part in components if not isinstance(part, Reply)]
                if with_component:
                    components.insert(0, reply)
                full = require(await self.bridge.capture(event))
                self.assertTrue(full.text.startswith("\n[引用消息]\n"))
                self.assertEqual(full.text.count(caption), 1)
                self.assertEqual((full.quotes[0].position, full.audios[0].descriptor.position), (0, 1))
                require(await session.receive(event, target))
                replay = await self.event(number + 1, text="继续", reply_to_message=self.message(number, voice=voice, caption=caption))
                require(await session.receive(replay, target))
                core = require(await self.bridge.capture_core_reply(event))
                self.assertEqual(core.text.count(caption), 1)
                with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
                    request, _ = await collect_initial_request(event, NS(), MainAgentBuildConfig(tool_call_timeout=5))
                    await _process_quote_message(event, request, "", NS())
                from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA, inject_core_reply_prompt
                event.set_extra(CORE_REPLY_CONTEXT_EXTRA, {"prepared": {"native_input": core}})
                inject_core_reply_prompt(event, request)
                inject_core_reply_prompt(event, request)
                payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
                self.assertEqual(payload.count(caption), 1)
                self.assertEqual(payload.count(quote_text), 1)

    async def test_transcribed_voice_reply_includes_caption_once(self):
        from pathlib import Path
        from telegram import Voice
        from astrbot.core.astr_main_agent import MainAgentBuildConfig, _process_quote_message, collect_initial_request
        from astrbot.core.message.components import Plain
        from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA, inject_core_reply_prompt
        from tests.audio_support import audio_bytes

        async def download(url, *, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(audio_bytes())

        transcript, caption, quote_text = "语音转写正文", "这是语音说明", "先前的答复"
        original = self.message(9, 424242, text=quote_text)
        converted = await self.event(90, text="继续", reply_to_message=original)
        reply = next(part for part in converted.message_obj.message if isinstance(part, Reply))
        with patch.object(Voice, "get_file", AsyncMock(return_value=NS(file_path="https://example.org/voice"))), \
                patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", download):
            for number, header in ((100, []), (110, [reply])):
                event = await self.event(number, voice={"file_id": "v", "file_unique_id": "v1", "duration": 1},
                                         caption=caption, reply_to_message=original)
                event.message_obj.message = [*header, Plain(transcript)]
                event.message_str = transcript
                core = require(await self.bridge.capture_core_reply(event))
                self.assertIn(caption, core.text)
                with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
                    request, _ = await collect_initial_request(event, NS(), MainAgentBuildConfig(tool_call_timeout=5))
                    await _process_quote_message(event, request, "", NS())
                event.set_extra(CORE_REPLY_CONTEXT_EXTRA, {"prepared": {"native_input": core}})
                inject_core_reply_prompt(event, request)
                payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
                for expected in (transcript, caption, quote_text):
                    self.assertEqual(payload.count(expected), 1, expected)

    async def test_admitted_attachment_quotes_match_sdk_originals(self):
        from contextlib import ExitStack
        from pathlib import Path
        from telegram import Document, Voice
        from astrbot.core.message.components import File, Record
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
        from astrbot_host.audio.loader import load_native_audio
        from tests.audio_support import audio_bytes
        from tests.test_conversation_images import image_bytes

        self.bridge.audio_type, self.bridge.audio_loader = Record, load_native_audio
        self.bridge.file_type = File
        session = self.controller.session
        target = self.controller.settings.target("tg-owner")
        bot = self.platform.client
        bot.base_file_url = f"https://api.telegram.org/file/bot{bot.token}"
        url = bot.base_file_url + "/synthetic/test.wav"
        thumb = dict(file_id="thumb", file_unique_id="t", width=2, height=2)
        sticker = dict(file_id="stk", file_unique_id="s", width=2, height=2, type="regular", emoji="🍀")
        caption = "😀一个片段"
        cases = [(dict(sticker=dict(sticker, is_video=video, is_animated=not video, thumbnail=preview)),
                  ("\n[图片 1]\n" if preview else "") + "[贴纸 🍀]")
                 for video in (True, False) for preview in (thumb, None)]
        cases += [(dict(voice=dict(file_id="v", file_unique_id="v1", duration=1), caption=caption),
                   "[语音消息]\n" + caption),
                  (dict(document=dict(file_id="d", file_unique_id="d1", file_name="notes.txt"), caption=caption),
                   "[文件 notes.txt]\n" + caption)]

        async def download(url, *, path):
            Path(path).write_bytes(audio_bytes())

        Path(get_astrbot_temp_path()).mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            # Exercise admission and durable reads without scheduling unrelated reply generation.
            stack.enter_context(patch.object(session.application, "worker_limit", 0))
            for cls in (Voice, Document, PhotoSize):
                stack.enter_context(patch.object(cls, "get_file", AsyncMock(return_value=NS(file_path=url))))
            stack.enter_context(patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", download))
            stack.enter_context(patch("astrbot_host.media.fetch_image", AsyncMock(return_value=image_bytes())))
            for index, (fields, expected) in enumerate(cases):
                with self.subTest(index=index):
                    number = 300 + index * 10
                    require(await session.receive(await self.event(number, **fields), target))
                    original = self.message(number, **fields)
                    event = await self.event(number + 1, text="继续", reply_to_message=original)
                    require(await session.receive(event, target))
                    core = require(await self.bridge.capture_core_reply(event))
                    self.assertEqual(core.text, "继续")
                    quote, = core.quotes
                    self.assertEqual((quote.text, quote.sender_id, quote.sent_at), (expected, str(OWNER), 1700000000))
                    self.assertEqual(self.observe(core)[0], "继续")
                    quoted_text = self.quoted_evidence(core)
                    self.assertIn(expected, quoted_text)
                    self.assertIn(str(OWNER), quoted_text)
                    self.assertIn("1700000000", quoted_text)
                    if "caption" in fields:
                        selection = await self.event(number + 2, text="选段", reply_to_message=original,
                                                     quote={"text": "片段", "position": 4})
                        require(await session.receive(selection, target))
                        self.assertEqual(require(await self.bridge.capture_core_reply(selection)).quotes[0].text, "片段")
                        changed = dict(original, caption="其他")
                        altered = await self.event(number + 4, text="选段", reply_to_message=dict(original, caption=caption + "改"),
                                                   quote={"text": "片段", "position": 4})
                        self.assertEqual((await session.receive(altered, target)).failure.code, "quote_original_changed")
                    else:
                        changed = dict(original, sticker=dict(fields["sticker"], emoji="😀"))
                    changed_event = await self.event(number + 3, text="?", reply_to_message=changed)
                    self.assertEqual((await session.receive(changed_event, target)).failure.code, "quote_original_changed")
        with self.controller.runtime.read_only_snapshot() as db:
            rows = "\n".join(db.iterdump())
        self.assertNotIn(bot.token, rows)
        self.assertNotIn("/synthetic/test.wav", rows)
