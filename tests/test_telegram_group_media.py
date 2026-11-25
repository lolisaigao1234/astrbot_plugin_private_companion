"""Group updates converted by AstrBot with offline, synthetic file contents."""

from dataclasses import replace
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_telegram_platform import OWNER, TelegramHostFixture
from tests.audio_support import audio_bytes
from tests.test_conversation_images import image_bytes
from tests.video_support import video_bytes
from telegram import Update
from astrbot.core.message.components import File, Image, Record, Video
from astrbot_host.audio.decoder import inspect_audio
from astrbot_host.audio.loader import load_native_audio
from astrbot_host.files.loader import load_native_file
from astrbot_host.files.parsing import extract_document
from astrbot_host.media import load_native_image
from astrbot_host.video.decoder import inspect_video
from astrbot_host.video.loader import load_native_video


class TelegramGroupMediaTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_failed_reads_reduce_group_file_allowance(self):
        from tests.telegram_failed_read_support import check_failed_reads

        message = await self.platform.convert_message(self.update(11, text="caption"), NS(bot=self.platform.client))
        event = self.platform.create_event(message)
        await check_failed_reads(self, event, self.bridge)

    async def test_provider_photo_budget(self):
        from tests.telegram_provider_photo_support import check_provider_photo_budget

        async def make_event(fields, original):
            update = self.update(11, **fields, reply_to_message=self.update(10, **original).message.to_dict())
            message = await self.platform.convert_message(update, NS(bot=self.platform.client))
            event = self.platform.create_event(message)
            event.created_at = self.clock.now().timestamp()
            return event

        await check_provider_photo_budget(self, make_event, "test-group", OWNER)

    async def test_provider_quoted_documents_and_videos(self):
        from tests.telegram_provider_photo_support import check_provider_quoted_attachments

        async def make_event(fields, original):
            update = self.update(11, **fields, reply_to_message=self.update(10, **original).message.to_dict())
            message = await self.platform.convert_message(update, NS(bot=self.platform.client))
            event = self.platform.create_event(message)
            event.created_at = self.clock.now().timestamp()
            return event

        await check_provider_quoted_attachments(self, make_event, "test-group")

    async def asyncSetUp(self):
        config = self.controller.config
        config["multi_host"]["group_targets"] = [dict(target_id="test-group", group_id="-400",
            platform_id="tg-main", account_id="tg-main", bot_id="bot-tg")]
        self.window_personas["tg-main:GroupMessage:-400"] = "original"
        self.types = replace(self.types, image=Image, image_loader=load_native_image, audio=Record,
                             audio_loader=load_native_audio, video=Video, video_loader=load_native_video,
                             file=File, file_loader=load_native_file)
        self.controller = self.make_controller(config)
        await self.start_host()
        self.bridge = self.controller.session.bridge.groups
        self.bridge.file_settings = lambda *_: {"file_understanding_enabled": True}
        bot = self.platform.client
        bot.base_file_url = f"https://api.telegram.org/file/bot{bot.token}"
        bot.get_file = AsyncMock(return_value=NS(file_path=bot.base_file_url + "/test/media"))

    def update(self, number, **fields):
        return Update.de_json({"update_id": number, "message": {"message_id": number, "date": 1700000000,
            "chat": {"type": "group", "id": -400}, "from": {"id": OWNER + number % 2, "is_bot": False,
            "first_name": "Synthetic"}, **fields}}, self.platform.client)

    async def capture(self, message):
        event = self.platform.create_event(message)
        captured = (await self.bridge.capture(event)).unwrap()
        self.assertEqual(captured.principal.native_account_id, message.sender.user_id)
        self.assertNotIn(self.platform.client.token, repr(captured))
        return captured

    async def test_captioned_group_photo_budget(self):
        await self.check_group_photo_budget("keep caption")

    async def test_uncaptioned_group_photo_budget(self):
        await self.check_group_photo_budget(None)

    async def check_group_photo_budget(self, caption):
        originals = [image_bytes("red"), image_bytes("blue")]
        items = [(self.update(n, photo=[dict(file_id=str(n), file_unique_id=str(n), width=2, height=3)],
                              media_group_id="album", caption=caption if n == 1 else None),
                  NS(bot=self.platform.client)) for n in (1, 3)]
        self.platform.media_group_cache = {"album": {"items": items}}
        self.platform.handle_msg = AsyncMock()
        await self.platform.process_media_group("album")
        message = self.platform.handle_msg.await_args.args[0]
        with patch("astrbot_host.media.fetch_image", AsyncMock(side_effect=originals)), patch(
                "companion_v2.conversation.media.models.MAX_TURN_BYTES", max(map(len, originals))):
            captured = await self.capture(message)
        self.assertEqual([item.content for item in captured.images], originals[:1])
        if caption:
            self.assertIn(caption, captured.text)
        self.assertIn("附件超过读取上限", captured.text)

        async def slow_photo(locator, **kwargs):
            if locator.position == captured.images[0].descriptor.position:
                return captured.images[0]
            await asyncio.Event().wait()

        with patch.object(self.bridge, "image_loader", slow_photo), patch(
                "companion_v2.adapters.astrbot.group.MEDIA_PREPARE_TIMEOUT", 0.1):
            timed = await self.capture(message)
        self.assertEqual(timed.images, captured.images)
        self.assertIn("附件读取超时", timed.text)
        if caption:
            self.assertIn(caption, timed.text)

        import base64
        from astrbot.core.astr_main_agent import collect_initial_request
        event = self.platform.create_event(message)
        event.created_at = self.clock.now().timestamp()
        target = self.controller.settings.target("test-group")
        downloads = iter(originals)
        async def download(url, dest):
            Path(dest).write_bytes(next(downloads))
        with patch("astrbot_host.media.fetch_image", AsyncMock(side_effect=originals)), patch(
                "companion_v2.conversation.media.models.MAX_TURN_BYTES", max(map(len, originals))), patch(
                "astrbot.core.utils.media_utils.download_file", download), patch(
                "astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            (await self.controller.session.prepare_core_reply(event, target)).unwrap()
            request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
            payload = await request.assemble_context()
        self.assertEqual([base64.b64decode(part["image_url"]["url"].split(",")[1])
                          for part in payload["content"] if part["type"] == "image_url"], originals[:1])
        self.assertIn("附件超过读取上限", request.prompt)
        if caption:
            self.assertIn(caption, request.prompt)

    async def test_group_oversized_video_keeps_caption(self):
        from companion_v2.execution.models import TaskError
        update = self.update(1, video=dict(file_id="v", file_unique_id="v1", width=64, height=48, duration=1),
                             caption="keep caption")
        message = await self.platform.convert_message(update, NS(bot=self.platform.client))
        with patch("astrbot_host.video.loader.fetch_attachment",
                   AsyncMock(side_effect=TaskError("host_image_limit", "synthetic transport limit"))):
            captured = await self.capture(message)
        self.assertEqual(captured.videos, ())
        self.assertIn("keep caption", captured.text)
        self.assertIn("附件超过读取上限", captured.text)

    async def test_group_quote_yields_photo_budget_to_current_sender(self):
        photo = [dict(file_id="photo", file_unique_id="p", width=2, height=2)]
        original = self.update(10, photo=photo, caption="source caption")
        reply = self.update(11, photo=photo, caption="current caption", reply_to_message=original.message.to_dict())
        message = await self.platform.convert_message(reply, NS(bot=self.platform.client))
        direct, quoted = image_bytes("red"), image_bytes("blue")
        with patch("astrbot_host.media.fetch_image", AsyncMock(side_effect=[direct, quoted])), patch(
                "companion_v2.conversation.media.models.MAX_TURN_BYTES", max(len(direct), len(quoted))):
            captured = await self.capture(message)
        self.assertEqual([image.content for image in captured.images], [direct])
        self.assertIn("current caption", captured.text)
        quote, = captured.quotes
        self.assertEqual((quote.message_id, quote.sender_id, quote.sent_at, quote.images),
                         ("10", str(OWNER), 1700000000, ()))
        self.assertIn("source caption", quote.text)

    async def test_direct_record_and_video_consume_capacity_before_quote(self):
        import base64
        from companion_v2.execution.models import TaskError

        photo = [dict(file_id="photo", file_unique_id="p", width=2, height=2)]
        original = self.update(10, photo=photo, caption="original")
        message = await self.platform.convert_message(self.update(
            11, photo=photo, caption="current", reply_to_message=original.message.to_dict()), NS(bot=self.platform.client))
        audio = audio_bytes()
        message.message.extend([Record(file="base64://" + base64.b64encode(audio).decode()),
                                Video(file="https://example.org/large"), Video(file="https://example.org/fit")])
        direct = image_bytes()
        clip = video_bytes()
        audio_cost = inspect_audio(audio, 0).descriptor.retained_bytes
        video_cost = inspect_video(clip, 0).descriptor.retained_bytes
        budget = len(direct) + audio_cost + video_cost + 1
        audio_loader = AsyncMock(wraps=load_native_audio)
        photo_fetch = AsyncMock(side_effect=[direct, TaskError("host_image_limit", "too large")])
        video_fetch = AsyncMock(side_effect=[TaskError("host_image_limit", "too large"), clip])
        with (patch.object(self.bridge, "audio_loader", audio_loader),
              patch("companion_v2.conversation.media.models.MAX_TURN_BYTES", budget),
              patch("astrbot_host.media.fetch_image", photo_fetch),
              patch("astrbot_host.video.loader.fetch_attachment", video_fetch),
              patch("astrbot_host.audio.loader.decode_audio", AsyncMock(side_effect=inspect_audio)),
              patch("astrbot_host.video.loader.decode_video", AsyncMock(side_effect=inspect_video))):
            captured = await self.capture(message)
        self.assertEqual([c.kwargs["maximum_bytes"] for c in photo_fetch.await_args_list], [budget, 1])
        self.assertEqual(audio_loader.await_args.kwargs["maximum_bytes"], audio_cost + video_cost + 1)
        self.assertEqual([c.kwargs["maximum_bytes"] for c in video_fetch.await_args_list], [video_cost + 1] * 2)
        self.assertEqual([v.content for v in captured.videos], [clip])
        self.assertEqual(captured.quotes[0].images, ())
        self.assertIn("original", captured.quotes[0].text)
        self.assertIn("current", captured.text)

    async def test_group_attachments_retain_the_speaking_member(self):
        async def download(url, *, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(audio_bytes())

        async def decode_document(content, locator, **options):
            return extract_document(content, locator.position, locator.name, **options)

        file = dict(file_id="fixture", file_unique_id="fixture1")
        cases = [
            (dict(photo=[dict(file, width=2, height=2)]), "images"),
            (dict(voice=dict(file, duration=1)), "audios"),
            (dict(video=dict(file, width=64, height=48, duration=1)), "videos"),
            (dict(document=dict(file, file_name="fixture.txt")), "document body"),
            (dict(sticker=dict(file, type="regular", height=48, width=48, is_animated=False,
                               is_video=False, emoji="😀")), "[贴纸 😀]"),
        ]
        with (patch("astrbot_host.media.fetch_image", AsyncMock(return_value=image_bytes())),
              patch("astrbot_host.audio.loader.decode_audio", AsyncMock(side_effect=inspect_audio)),
              patch("astrbot_host.video.loader.decode_video", AsyncMock(side_effect=inspect_video)),
              patch("astrbot_host.video.loader.fetch_attachment", AsyncMock(return_value=video_bytes())),
              patch("astrbot_host.files.loader.decode_document", decode_document),
              patch("astrbot_host.files.loader.fetch_attachment", AsyncMock(return_value=b"document body")),
              patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", download)):
            for i, (fields, expected) in enumerate(cases, 1):
                with self.subTest(expected=expected):
                    message = await self.platform.convert_message(self.update(i, **fields), NS(bot=self.platform.client))
                    captured = await self.capture(message)
                    self.assertEqual(captured.principal.native_account_id, str(OWNER + i % 2))
                    if expected in {"images", "audios", "videos"}:
                        self.assertEqual(len(getattr(captured, expected)), 1)
                    else:
                        self.assertIn(expected, captured.text)

    async def test_group_quote_of_document_or_forward_with_bot_mention(self):
        async def receive(update):
            message = await self.platform.convert_message(update, NS(bot=self.platform.client))
            event = self.platform.create_event(message)
            event.created_at = self.clock.now().timestamp()
            return (await self.controller.session.receive(event, self.controller.settings.target("test-group"))).unwrap()

        async def decode(content, locator, **options):
            return extract_document(content, locator.position, locator.name, **options)

        body = "😀 @Synthetic_bot 看看"
        entities = [dict(type="mention", offset=3, length=14)]
        with (patch("astrbot_host.files.loader.decode_document", decode),
              patch("astrbot_host.files.loader.fetch_attachment", AsyncMock(return_value=b"document body"))):
            for number, fields in (
                (10, dict(document=dict(file_id="f", file_unique_id="f1", file_name="fixture.txt"),
                          caption=body, caption_entities=entities)),
                (20, dict(text=body, entities=entities, forward_origin={"type": "user", "date": 1600000000,
                          "sender_user": {"id": 321, "first_name": "Source", "is_bot": False}})),
            ):
                original = self.update(number, **fields)
                await receive(original)
                for offset, quote in ((1, {}), (2, {"quote": dict(text="看看", position=18)})):
                    with self.subTest(message_id=number, partial=bool(quote)):
                        await receive(self.update(number + offset, text="怎么看", reply_to_message=original.message.to_dict(), **quote))

    async def test_group_voice_caption_and_link_match_a_full_quote(self):
        async def download(url, *, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(audio_bytes())

        async def receive(update):
            converted = await self.platform.convert_message(update, NS(bot=self.platform.client))
            event = self.platform.create_event(converted)
            event.created_at = self.clock.now().timestamp()
            return (await self.controller.session.receive(event, self.controller.settings.target("test-group"))).unwrap()

        original = self.update(10, reply_to_message=self.update(7, text="先前的原文").message.to_dict(),
                               voice=dict(file_id="voice", file_unique_id="v1", duration=1),
                               caption="😀 guide", caption_entities=[
                                   dict(type="text_link", offset=3, length=5, url="https://example.org/guide")])
        with (patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", download),
              patch("astrbot_host.audio.loader.decode_audio", AsyncMock(side_effect=inspect_audio))):
            await receive(original)
            for number, extra in ((11, {}), (12, {"quote": dict(text="guide", position=3)})):
                await receive(self.update(number, text="怎么看", reply_to_message=original.message.to_dict(), **extra))

    async def test_provider_prompt_preserves_forward_and_discards_unavailable_quote(self):
        import json
        from astrbot.core.astr_main_agent import MainAgentBuildConfig, _process_quote_message, collect_initial_request
        from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA, inject_core_reply_prompt
        from astrbot_host.core_reply_mentions import restore_group_mentions

        anonymous = self.update(8, text="匿名引用原文", sender_chat={"id": -400, "type": "group"}).message
        for fields, marker in (
            ({"forward_origin": {"type": "hidden_user", "sender_user_name": "原作者", "date": 1600000000}}, "[转发消息，来源：原作者]"),
            ({"reply_to_message": anonymous.to_dict()}, "[引用消息不可用]"),
        ):
            update = self.update(12, text="看 @Synthetic_bot 正文", entities=[
                dict(type="mention", offset=2, length=14)], **fields)
            message = await self.platform.convert_message(update, NS(bot=self.platform.client))
            event = self.platform.create_event(message)
            event.created_at = self.clock.now().timestamp()
            native = (await self.bridge.capture_text_only(event)).unwrap()
            with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
                request, _ = await collect_initial_request(event, NS(), MainAgentBuildConfig(tool_call_timeout=5))
                await _process_quote_message(event, request, "", NS())
            event.set_extra(CORE_REPLY_CONTEXT_EXTRA, {"prepared": {"native_input": native}})
            inject_core_reply_prompt(event, request)
            restore_group_mentions(event, request, native, NS())
            payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
            self.assertEqual(payload.count(marker), 1)
            self.assertIn("看 <提及自己> 正文", payload)
            self.assertNotIn("匿名引用原文", payload)

    async def test_group_turn_text_is_the_members_own(self):
        from astrbot.core.astr_main_agent import collect_initial_request
        from astrbot_host.core_reply_mentions import restore_group_mentions
        from companion_v2.conversation.group_timing import SELF_MENTION
        from companion_v2.execution.models import TaskError

        async def download(url, *, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(audio_bytes())

        mention = [dict(type="mention", offset=2, length=14)]
        note = "\n[附件超过读取上限，本轮未读取]"
        for number, fields, expected in (
                (11, dict(photo=[dict(file_id="photo", file_unique_id="p", width=2, height=3)]), note),
                (12, dict(voice=dict(file_id="voice", file_unique_id="v1", duration=1)), "")):
            with self.subTest(number=number):
                update = self.update(number, caption="看 @Synthetic_bot 这个", caption_entities=mention, **fields)
                with (patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", download),
                      patch("astrbot_host.media.fetch_image",
                            AsyncMock(side_effect=TaskError("host_image_limit", "synthetic limit"))),
                      patch("astrbot.core.astr_main_agent._get_session_conv",
                            AsyncMock(return_value=NS(history="[]")))):
                    message = await self.platform.convert_message(update, NS(bot=self.platform.client))
                    event = self.platform.create_event(message)
                    event.created_at = self.clock.now().timestamp()
                    target = self.controller.settings.target("test-group")
                    prepared = (await self.controller.session.prepare_core_reply(event, target)).unwrap()
                    request, _ = await collect_initial_request(
                        event, NS(), NS(provider_wake_prefix="", provider_settings={}))
                native = prepared["native_input"]
                self.assertEqual(native.text, "看 @Synthetic_bot 这个")
                restore_group_mentions(event, request, native, NS())
                self.assertEqual(request.prompt, f"看 {SELF_MENTION} 这个{expected}")

    async def test_caption_keeps_mention_link_and_photo(self):
        from companion_v2.conversation.group_timing import SELF_MENTION

        url = "https://example.org/guide"
        update = self.update(1, photo=[dict(file_id="photo", file_unique_id="p", width=2, height=3)],
                             caption="😀 @Synthetic_bot guide", caption_entities=[
                                 dict(type="mention", offset=3, length=14),
                                 dict(type="text_link", offset=18, length=5, url=url)])
        message = await self.platform.convert_message(update, NS(bot=self.platform.client))
        with patch("astrbot_host.media.fetch_image", AsyncMock(return_value=image_bytes())):
            captured = await self.capture(message)
        self.assertEqual(len(captured.images), 1)
        self.assertIn(f"😀 {SELF_MENTION} guide ({url})", captured.text)

    async def test_group_album_keeps_both_photos_and_caption(self):
        items = [(self.update(n, photo=[dict(file_id=str(n), file_unique_id=str(n), width=2, height=3)],
                             media_group_id="album", caption="相册" if n == 1 else None), NS(bot=self.platform.client))
                 for n in (1, 3)]
        self.platform.media_group_cache = {"album": {"items": items}}
        self.platform.handle_msg = AsyncMock()
        await self.platform.process_media_group("album")
        with patch("astrbot_host.media.fetch_image", AsyncMock(side_effect=[image_bytes("red"), image_bytes("blue")])):
            captured = await self.capture(self.platform.handle_msg.await_args.args[0])
        self.assertEqual(len(captured.images), 2)
        self.assertIn("相册", captured.text)

    async def test_forward_keeps_origin_separate_from_the_group_speaker(self):
        update = self.update(1, text="转发正文", forward_origin={"type": "user", "date": 1600000000,
            "sender_user": {"id": 321, "first_name": "来源", "is_bot": False}})
        message = await self.platform.convert_message(update, NS(bot=self.platform.client))
        captured = await self.capture(message)
        node, = captured.forwards[0].nodes
        self.assertEqual((node.sender_id, node.text), ("321", "转发正文"))
        self.assertEqual(captured.principal.native_account_id, str(OWNER + 1))
