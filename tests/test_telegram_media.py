"""Synthetic Telegram private attachments against the installed AstrBot adapter."""

from tests import astrbot_data_root  # noqa: F401 - isolate SDK data before import

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update

from astrbot.core.message.components import File, Image, Plain, Record, Video
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from astrbot_host.audio.loader import load_native_audio
from tests.audio_support import audio_bytes
from tests.test_telegram_platform import OWNER, TelegramHostFixture, telegram_event


class TelegramCaptureTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.start_host()
        self.bridge = self.controller.session.bridge
        self.bridge.audio_type = Record
        self.bridge.audio_loader = load_native_audio
        self.event = telegram_event(self.platform)
        self.event.created_at = self.clock.now().timestamp()

    async def test_voice_reads_only_astrbot_temp_recordings(self):
        temp = Path(get_astrbot_temp_path())
        temp.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=temp) as owned, TemporaryDirectory() as other:
            inside = Path(owned, "voice.wav")
            outside = Path(other, "outside.wav")
            link = Path(owned, "link.wav")
            inside.write_bytes(audio_bytes())
            outside.write_bytes(audio_bytes())
            link.symlink_to(outside)
            for path in (inside, outside, link):
                self.event.message_obj.message = [Record(file=str(path), path=str(path), url=str(path))]
                result = await self.bridge.capture(self.event)
                if path == inside:
                    self.assertEqual(result.unwrap().audios[0].content, inside.read_bytes())
                else:
                    self.assertEqual(result.failure.code, "host_audio_location_invalid")

    async def convert(self, **fields):
        bot = self.platform.client
        bot.username = "synthetic_bot"
        bot.base_file_url = f"https://api.telegram.org/file/bot{bot.token}"
        bot.get_file = AsyncMock(return_value=NS(file_path=bot.base_file_url + "/test/media"))
        update = Update.de_json({"update_id": 1, "message": {"message_id": 1, "date": 1,
            "chat": {"type": "private", "id": OWNER},
            "from": {"id": OWNER, "is_bot": False, "first_name": "Synthetic"}, **fields}}, bot)
        self.event.message_obj = await self.platform.convert_message(update, NS(bot=bot))
        return self.event

    async def test_sticker_metadata_survives_conversion_and_capture(self):
        from astrbot_host.media import load_native_image
        from tests.test_conversation_images import image_bytes
        self.bridge.image_type, self.bridge.image_loader = Image, load_native_image
        thumb = dict(file_id="thumb", file_unique_id="thumb1", width=32, height=32)
        for animated, video in ((False, False), (True, False), (False, True)):
            await self.convert(sticker=dict(file_id="sticker", file_unique_id="s1", type="regular",
                height=48, width=48, is_animated=animated, is_video=video, emoji="😀", thumbnail=thumb))
            with patch("astrbot_host.media.fetch_image", AsyncMock(return_value=image_bytes())):
                captured = (await self.bridge.capture(self.event)).unwrap()
            self.assertEqual(len(captured.images), 1)
            for text in (captured.text, (await self.bridge.capture_core_reply(self.event)).unwrap().text):
                self.assertIn("[贴纸 😀]", text)
                self.assertNotIn("Sticker:", text)

    async def test_image_tools_retain_only_a_token_free_attachment_location(self):
        from astrbot_host.core_reply_media import frozen_image_locators
        from astrbot_host.media import load_native_image
        from tests.test_conversation_images import image_bytes
        await self.convert(photo=[dict(file_id="photo", file_unique_id="p1", height=2, width=3)])
        locator, = frozen_image_locators(self.event)
        self.assertNotIn(self.platform.client.token, repr(locator))
        with patch("astrbot_host.media.fetch_image", AsyncMock(return_value=image_bytes())) as fetch:
            captured = await load_native_image(locator)
        self.assertEqual(fetch.await_args.args[0], self.platform.client.base_file_url + "/test/media")
        self.assertNotIn(self.platform.client.token, repr(captured.descriptor))

    async def test_attachment_address_is_absent_from_receipts_logs_database_and_preview(self):
        from astrbot_host.files.loader import load_native_file
        self.bridge.file_type, self.bridge.file_loader = File, load_native_file
        self.bridge.file_settings = lambda *_: {"file_understanding_enabled": True}
        await self.convert(document=dict(file_id="doc", file_unique_id="d1", file_name="demo.txt"),
                           caption="keep this caption")
        location = self.platform.client.base_file_url + "/test/media"
        with patch("astrbot_host.files.loader.fetch_attachment", AsyncMock(side_effect=OSError(location))):
            with self.assertLogs("companion_v2.adapters.astrbot.files", "WARNING") as logs:
                batch = (await self.controller.receive_claimed(self.event)).unwrap()
        preview = self.controller.preview("tg-owner", batch).unwrap()
        self.assertEqual(preview["batch_id"], batch)
        with self.controller.runtime.read_only_snapshot() as db:
            rows = "\n".join(db.iterdump())
        self.assertTrue("keep this caption" in rows, "accepted caption missing from storage")
        self.assertTrue("文件读取失败" in rows, "safe file error missing from storage")
        for output in (logs.output, rows, preview):
            self.assertFalse(self.platform.client.token in str(output), "credential in companion output")

    async def test_document_retains_text_and_caption(self):
        from astrbot_host.files.loader import load_native_file
        self.bridge.file_type, self.bridge.file_loader = File, load_native_file
        self.bridge.file_settings = lambda *_: {"file_understanding_enabled": True}
        await self.convert(document=dict(file_id="doc", file_unique_id="d1", file_name="demo.txt"),
                           caption="caption")
        with patch("astrbot_host.files.loader.fetch_attachment", AsyncMock(return_value=b"document body")):
            captured = (await self.bridge.capture(self.event)).unwrap()
        self.assertIn("document body", captured.text)
        self.assertIn("caption", captured.text)
        self.assertNotIn(self.platform.client.token, captured.text)
        self.bridge.file_settings = lambda *_: {"file_understanding_enabled": True, "file_understanding_max_size_mb": 1}
        with patch("astrbot_host.media.aiohttp.TCPConnector"), patch("astrbot_host.media.aiohttp.ClientSession") as session:
            response = NS(status=200, headers={}, content_length=1024**2 + 1)
            request = MagicMock()
            request.return_value.__aenter__.return_value = response
            session.return_value.__aenter__.return_value.get = request
            captured = (await self.bridge.capture(self.event)).unwrap()
        self.assertIn("caption", captured.text)
        self.assertIn("文件超过读取大小上限", captured.text)

    async def test_video_and_video_note_are_understood(self):
        from astrbot_host.video.loader import load_native_video
        from tests.video_support import video_bytes
        self.bridge.video_type, self.bridge.video_loader = Video, load_native_video
        common = dict(file_id="video", file_unique_id="v1", duration=1)
        for fields in (dict(video=dict(common, width=64, height=48), caption="video caption"),
                       dict(video_note=dict(common, length=64))):
            await self.convert(**fields)
            with patch("astrbot_host.video.loader.fetch_attachment", AsyncMock(return_value=video_bytes())):
                captured = (await self.bridge.capture(self.event)).unwrap()
            self.assertEqual(len(captured.videos), 1)
            self.assertTrue(captured.videos[0].descriptor.frames)
            self.assertNotIn(self.platform.client.token, repr(captured))
            from companion_v2.execution.models import TaskError
            too_large = TaskError("host_image_limit", "synthetic transport byte limit")
            with patch("astrbot_host.video.loader.fetch_attachment", AsyncMock(side_effect=too_large)):
                oversized = await self.bridge.capture(self.event)
            if "caption" in fields:
                self.assertIn(fields["caption"], oversized.unwrap().text)
                self.assertEqual(oversized.unwrap().videos, ())
            else:
                self.assertEqual(oversized.failure.code, "host_video_limit")

    async def test_album_keeps_two_original_photos_and_caption(self):
        from astrbot_host.media import load_native_image
        from tests.test_conversation_images import image_bytes
        self.bridge.image_type, self.bridge.image_loader = Image, load_native_image
        items = []
        for number in (1, 2):
            await self.convert(photo=[dict(file_id=str(number), file_unique_id=str(number), width=3, height=2)],
                               media_group_id="album", caption="album caption" if number == 1 else None)
            items.append((self.event.message_obj.raw_message, NS(bot=self.platform.client)))
        self.platform.media_group_cache = {"album": {"items": items}}
        self.platform.handle_msg = AsyncMock()
        await self.platform.process_media_group("album")
        self.event.message_obj = self.platform.handle_msg.await_args.args[0]
        with patch("astrbot_host.media.fetch_image", AsyncMock(side_effect=[image_bytes("red"), image_bytes("blue")])):
            captured = (await self.bridge.capture(self.event)).unwrap()
        self.assertEqual([image.descriptor.position for image in captured.images], [0, 2])
        self.assertIn("album caption", captured.text)
        # Each image fits alone. Only the latter crosses the shared byte limit.
        budget = max(image.descriptor.retained_bytes for image in captured.images)
        with patch("companion_v2.conversation.media.models.MAX_TURN_BYTES", budget), patch(
                "astrbot_host.media.fetch_image", AsyncMock(side_effect=[image_bytes("red"), image_bytes("blue")])):
            partial = (await self.bridge.capture(self.event)).unwrap()
        self.assertEqual([image.descriptor.position for image in partial.images], [0])
        self.assertEqual(partial.text.count("附件超过读取上限"), 1)
        self.assertIn("album caption", partial.text)

        from companion_v2.execution.models import TaskError
        too_large = TaskError("host_image_limit", "oversized synthetic photo")
        with patch("astrbot_host.media.fetch_image", AsyncMock(side_effect=[too_large, image_bytes()])):
            captured = (await self.bridge.capture(self.event)).unwrap()
        self.assertEqual([image.descriptor.position for image in captured.images], [2])
        self.assertIn("album caption", captured.text)

        # Exercise the managed reply entry and the SDK's serialized provider request.
        from astrbot.core.astr_main_agent import collect_initial_request
        from astrbot_host.telegram_request import protect_telegram_request

        event = self.platform.create_event(self.event.message_obj)
        event.created_at = self.clock.now().timestamp()
        originals = [image_bytes("red"), image_bytes("blue")]
        sdk_images = iter(originals)
        async def download(url, dest):
            Path(dest).write_bytes(next(sdk_images))
        with patch("companion_v2.conversation.media.models.MAX_TURN_BYTES", budget), patch(
                "astrbot_host.media.fetch_image", AsyncMock(side_effect=originals)), patch(
                "astrbot.core.utils.media_utils.download_file", download), patch(
                "astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            plugin = self.started_plugin()
            await plugin.receive_companion_message(event)
            request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
            protect_telegram_request(event, request)
            message = await request.assemble_context()
        serialized = json.dumps(message, ensure_ascii=False)
        images = [base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                  for part in message["content"] if part["type"] == "image_url"]
        self.assertEqual(images, originals[:1])
        self.assertIn("album caption", serialized)
        self.assertIn("附件超过读取上限", serialized)
        self.assertNotIn(self.platform.client.token, serialized)

    async def test_voice_and_music_from_converted_updates(self):
        temp = Path(get_astrbot_temp_path())
        temp.mkdir(parents=True, exist_ok=True)

        async def synthetic_download(url, *, path):
            Path(path).write_bytes(audio_bytes())

        media = dict(file_id="sound", file_unique_id="a1", duration=1)
        for fields in (dict(voice=media), dict(audio=media, caption="music caption")):
            with patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", synthetic_download):
                await self.convert(**fields)
            captured = (await self.bridge.capture(self.event)).unwrap()
            self.assertEqual(captured.audios[0].waveform, audio_bytes())
            if "audio" in fields:
                self.assertIn("music caption", captured.text)

    async def test_oversized_music_preserves_its_caption(self):
        from companion_v2.conversation.media.audio.models import MAX_AUDIO_BYTES
        temp = Path(get_astrbot_temp_path())
        temp.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=temp) as owned:
            path = Path(owned, "music.wav")
            with path.open("wb") as stream:
                stream.truncate(MAX_AUDIO_BYTES + 1)
            self.event.message_obj.message = [Record(file=str(path), path=str(path)), Plain("caption")]
            with patch("astrbot_host.audio.loader.decode_audio", new_callable=AsyncMock) as decode:
                captured = (await self.bridge.capture(self.event)).unwrap()
                decode.assert_not_awaited()
            self.assertIn("caption", captured.text)
            self.assertEqual(captured.audios, ())


class TelegramWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_picture_and_reaction_use_native_photo_and_animation_sends(self):
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot_host.core_reply_media_delivery import native_reply_messages, native_wire_message
        from tests.test_conversation_images import image_bytes
        from tests.test_telegram_platform import TelegramPlatform

        platform = TelegramPlatform()
        event = telegram_event(platform)
        platform.client.send_photo = AsyncMock()
        platform.client.send_animation = AsyncMock()
        platform.client.send_chat_action = AsyncMock()
        with TemporaryDirectory() as folder:
            photo_path = Path(folder, "photo.png")
            sticker_path = Path(folder, "sticker.gif")
            photo_path.write_bytes(image_bytes())
            sticker_path.write_bytes(image_bytes(kind="GIF"))
            sticker = Image.fromFileSystem(str(sticker_path))
            object.__setattr__(sticker, "_companion_v2_reaction_attachment", {"image_format": "qq_emoji"})
            message = MessageChain(chain=[Image.fromFileSystem(str(photo_path)), sticker])
            for part in native_reply_messages(event, message):
                await event.send(await native_wire_message(event, part))
        platform.client.send_photo.assert_awaited_once()
        platform.client.send_animation.assert_awaited_once()

    async def test_provider_request_drops_credentials_from_sdk_placeholders(self):
        from astrbot.core.agent.message import TextPart
        from astrbot.core.provider.entities import ProviderRequest
        from tests.test_telegram_platform import TelegramPlatform
        from tests.test_main_no_reply import ENTRY

        event = telegram_event(TelegramPlatform())
        url = f"https://api.telegram.org/file/bot{event.client.token}/video/test.mp4"
        request = ProviderRequest(prompt=url, contexts=[{"role": "user", "content": url}],
                                  extra_user_content_parts=[TextPart(text=url)], image_urls=[url])
        plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        await plugin.restate_companion_core_video_state(event, request)
        await plugin.restate_companion_core_video_state(event, request)
        self.assertEqual([part.text for part in request.extra_user_content_parts].count("[附件不可用]"), 1)
        for value in (request.prompt, request.contexts, request.extra_user_content_parts, request.image_urls):
            self.assertNotIn(event.client.token, str(value))

    async def test_reply_face_and_voice_reach_telegram(self):
        from astrbot.core.message.components import Face, At
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot_host.core_reply_media_delivery import native_reply_messages, native_wire_message
        from tests.test_telegram_platform import TelegramPlatform

        platform = TelegramPlatform()
        event = telegram_event(platform)
        platform.client.send_chat_action = AsyncMock()
        platform.client.send_message = AsyncMock()
        platform.client.send_voice = AsyncMock()
        with TemporaryDirectory() as temp:
            path = Path(temp, "record.wav")
            path.write_bytes(audio_bytes())
            for part in (Face(id=14), Record(file=str(path))):
                message = MessageChain(chain=[part])
                await event.send(await native_wire_message(event, message))
            platform.client.send_message.assert_awaited_once()
            self.assertIn("14", platform.client.send_message.await_args.kwargs["text"])
            sent = Path(platform.client.send_voice.await_args.kwargs["voice"]).read_bytes()
            self.assertEqual(sent[:4], b"OggS")
            self.assertIn(b"OpusHead", sent[:100])
            self.assertEqual(native_reply_messages(event, MessageChain(chain=[At(qq="42")])), ())
