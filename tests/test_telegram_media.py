"""Synthetic Telegram private attachments against the installed AstrBot adapter."""

from tests import astrbot_data_root  # noqa: F401 - isolate SDK data before import

import asyncio
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
import wave
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update

from astrbot.core.message.components import File, Image, Plain, Record, Video
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from astrbot_host.audio.loader import load_native_audio
from tests.audio_support import audio_bytes
from tests.test_telegram_fork_platform import OWNER, TelegramHostFixture, telegram_event


class TelegramCaptureTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.start_host()
        self.bridge = self.controller.session.bridge
        self.bridge.audio_type = Record
        self.bridge.audio_loader = load_native_audio
        self.event = telegram_event(self.platform)
        self.event.created_at = self.clock.now().timestamp()

    async def test_failed_reads_reduce_capture_and_sdk_allowance(self):
        from astrbot_host.files.loader import load_native_file
        from astrbot_host.media import load_native_image
        from astrbot_host.video.loader import load_native_video
        from tests.telegram_failed_read_support import check_failed_reads

        self.bridge.image_type, self.bridge.image_loader = Image, load_native_image
        self.bridge.video_type, self.bridge.video_loader = Video, load_native_video
        self.bridge.file_type, self.bridge.file_loader = File, load_native_file
        self.bridge.file_settings = lambda *_: {"file_understanding_enabled": True}
        for sdk in (False, True):
            with self.subTest(sdk=sdk):
                await self.convert(text="caption")
                await check_failed_reads(self, self.event, self.bridge, sdk=sdk)

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

    async def test_standalone_video_respects_turn_limit_before_sdk_request(self):
        from astrbot.core.astr_main_agent import collect_initial_request
        from companion_v2.conversation.media.video.models import MAX_VIDEO_BYTES
        from companion_v2.execution.models import TaskError
        await self.convert(video=dict(file_id="v", file_unique_id="v", duration=1, width=64, height=48),
                           caption="my video caption")
        self.event = self.platform.create_event(self.event.message_obj)
        self.event.created_at = self.clock.now().timestamp()
        target = self.controller.settings.target("tg-owner")
        load = AsyncMock(side_effect=TaskError("host_video_limit", "synthetic oversized video"))
        with patch("astrbot_host.video.loader.fetch_native_video_bytes", load), patch(
                "astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            (await self.controller.session.prepare_core_reply(self.event, target)).unwrap()
            request, _ = await collect_initial_request(self.event, NS(), NS(provider_wake_prefix="", provider_settings={}))
            payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
        self.assertEqual(load.await_args.kwargs["maximum_bytes"], MAX_VIDEO_BYTES)
        self.assertIn("my video caption", payload)
        self.assertIn("附件超过读取上限", payload)
        self.assertFalse(any(isinstance(part, Video) for part in self.event.message_obj.message))

    async def test_gif_and_video_sent_as_file_reach_video_understanding(self):
        from astrbot.core.astr_main_agent import collect_initial_request
        from astrbot_host.core_reply_perception import video_placeholders
        target = self.controller.settings.target("tg-owner")
        clip = dict(file_id="v", file_unique_id="v", width=64, height=48, duration=1)
        for number, fields in enumerate((dict(animation=clip, document=dict(file_id="v", file_unique_id="v",
                                                                            file_name="clip.mp4")),
                                         dict(document=dict(file_id="v", file_unique_id="v", file_name="trip v1.2.MOV")))):
            await self.convert(**fields, caption="my caption", message_id=720 + number)
            event = self.platform.create_event(self.event.message_obj)
            event.created_at = self.clock.now().timestamp()
            with patch("astrbot_host.video.loader.fetch_attachment", AsyncMock(return_value=b"movie")), patch(
                    "astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
                (await self.controller.session.prepare_core_reply(event, target)).unwrap()
                request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
                payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
            item, = video_placeholders(request, include_quoted=False)
            self.assertEqual((item.name, item.location), (fields["document"]["file_name"], "path"))
            self.assertEqual(Path(item.value).read_bytes(), b"movie")
            self.assertIn("my caption", payload)
            self.assertNotIn(self.platform.client.token, payload)

    async def test_standalone_oversized_photo_and_music_keep_their_caption(self):
        from astrbot.core.astr_main_agent import collect_initial_request
        from astrbot_host.media import load_native_image
        from companion_v2.conversation.media.audio.models import MAX_AUDIO_BYTES
        from companion_v2.conversation.media.models import MAX_IMAGE_BYTES

        self.bridge.image_type, self.bridge.image_loader = Image, load_native_image
        target = self.controller.settings.target("tg-owner")

        async def oversized_download(_, path):
            with wave.open(str(path), "wb") as stream:
                stream.setparams((2, 2, 48000, 0, "NONE", ""))
                stream.writeframes(bytes(4 * MAX_AUDIO_BYTES))

        fetch = AsyncMock(return_value=b"x" * (MAX_IMAGE_BYTES + 1))
        for number, fields in enumerate((dict(photo=[dict(file_id="p", file_unique_id="p", width=3, height=2)]),
                                         dict(audio=dict(file_id="a", file_unique_id="a", duration=1)))):
            with patch("astrbot.core.platform.sources.telegram.tg_adapter.download_file", oversized_download):
                await self.convert(**fields, caption="my caption", message_id=700 + number)
            event = self.platform.create_event(self.event.message_obj)
            event.created_at = self.clock.now().timestamp()
            with patch("astrbot_host.media.fetch_image", fetch), patch(
                    "astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
                (await self.controller.session.prepare_core_reply(event, target)).unwrap()
                request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
                payload = json.dumps(await request.assemble_context(), ensure_ascii=False)
            self.assertIn("my caption", payload)
            self.assertIn("附件", payload)
            self.assertFalse(any(isinstance(part, (Image, Record)) for part in event.message_obj.message))
            self.assertFalse(request.image_urls or request.audio_urls)
            self.assertNotIn(self.platform.client.token, payload)
        fetch.assert_awaited_once()

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
        first_photo = captured.images[0]
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

        # A slow second photo must not discard the caption or the admitted first one.
        async def slow_photo(locator, **kwargs):
            if locator.position == 0:
                return first_photo
            await asyncio.Event().wait()

        with patch.object(self.bridge, "image_loader", slow_photo), patch(
                "companion_v2.adapters.astrbot.native.MEDIA_PREPARE_TIMEOUT", 0.1):
            timed = (await self.bridge.capture(self.event)).unwrap()
        self.assertEqual(timed.images, (first_photo,))
        self.assertIn("album caption", timed.text)
        self.assertIn("附件读取超时", timed.text)

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

    async def test_sdk_album_keeps_caption_and_first_photo_after_deadline(self):
        from astrbot.core.astr_main_agent import collect_initial_request
        from astrbot_host.media import load_native_image
        from tests.test_conversation_images import image_bytes

        await self.convert(photo=[dict(file_id="1", file_unique_id="1", width=3, height=2)],
                           caption="slow album caption", media_group_id="album")
        self.event.message_obj.message.extend([Image.fromURL("https://example.org/slow"),
                                               Image.fromURL("https://example.org/third")])
        event = self.platform.create_event(self.event.message_obj)
        event.created_at = self.clock.now().timestamp()
        payload = image_bytes("red")
        positions = []

        async def load(locator, **kwargs):
            positions.append(locator.position)
            if len(positions) == 1:
                return await load_native_image(locator, **kwargs)
            await asyncio.Event().wait()

        with patch.object(self.bridge, "image_loader", load), patch(
                "astrbot_host.media.fetch_image", AsyncMock(return_value=payload)), patch(
                "companion_v2.adapters.astrbot.native.MEDIA_PREPARE_TIMEOUT", 0.2), patch(
                "astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            await asyncio.wait_for(self.started_plugin().receive_companion_message(event), 5)
            request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
            message = await request.assemble_context()
        self.assertEqual(len(positions), 2)
        images = [base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                  for part in message["content"] if part["type"] == "image_url"]
        self.assertEqual(images, [payload])
        self.assertIn("slow album caption", str(message))
        self.assertIn("附件读取超时", str(message))

    async def test_managed_album_document_obeys_window_settings_before_fetch(self):
        target = self.controller.settings.target("tg-owner")
        for enabled, scope_allowed, name, expected in (
                (False, True, "note.txt", None), (True, False, "note.txt", None),
                (True, True, "note.exe", None), (True, True, "note.txt", 1024**2)):
            with self.subTest(enabled=enabled, scope_allowed=scope_allowed, name=name):
                event = await self.convert(document=dict(file_id="d", file_unique_id="d", file_name=name),
                                           caption="keep caption", media_group_id="docs")
                event = self.platform.create_event(event.message_obj)
                event.created_at = self.clock.now().timestamp()
                resolver = MagicMock(return_value={"file_understanding_enabled": enabled,
                    "file_understanding_private_enabled": scope_allowed, "file_understanding_max_size_mb": 1})
                with patch.object(self.bridge, "file_settings", resolver), patch(
                        "astrbot_host.media.fetch_attachment", AsyncMock(return_value=b"text")) as fetch:
                    prepared = (await self.controller.session.prepare_core_reply(event, target)).unwrap()
                self.assertIn("keep caption", event.message_str)
                context = prepared["context"]
                resolver.assert_called_with(context.scope, context.role_binding.role)
                if expected is None:
                    fetch.assert_not_awaited()
                    self.assertFalse(any(isinstance(part, File) for part in event.message_obj.message))
                else:
                    self.assertEqual(fetch.await_args.kwargs["maximum_bytes"], expected)
                    self.assertTrue(any(isinstance(part, File) for part in event.message_obj.message))

    async def test_album_documents_share_bytes_on_normal_provider_entry(self):
        from astrbot.core.astr_main_agent import collect_initial_request
        from astrbot_host.media import load_native_image
        from astrbot_host.telegram_request import protect_telegram_request
        from tests.test_conversation_images import image_bytes

        await self.convert(photo=[dict(file_id="1", file_unique_id="1", width=3, height=2)],
                           caption="album reading", media_group_id="album")
        event = self.platform.create_event(self.event.message_obj)
        event.created_at = self.clock.now().timestamp()
        event.message_obj.message.extend(File(name=f"{n}.txt", url=f"https://example.org/{n}")
                                         for n in ("first", "second", "third"))
        calls = []

        async def fetch(url, *, maximum_bytes):
            calls.append((url, maximum_bytes))
            return b"abc" if url.endswith("first") else b"de"

        self.bridge.image_type, self.bridge.image_loader = Image, load_native_image
        photo = image_bytes("red")
        with patch("companion_v2.conversation.media.models.MAX_TURN_BYTES", len(photo) + 5), patch.object(
                self.bridge, "file_settings", return_value={"file_understanding_enabled": True}), patch(
                "astrbot_host.media.fetch_image", AsyncMock(return_value=photo)), patch(
                "astrbot_host.media.fetch_attachment", fetch), patch(
                "astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            await self.started_plugin().receive_companion_message(event)
            request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
            protect_telegram_request(event, request)
            content = (await request.assemble_context())["content"]
        self.assertEqual(calls, [("https://example.org/first", 5), ("https://example.org/second", 2)])
        text = " ".join(p.get("text", "") for p in content)
        self.assertIn("album reading", text)
        self.assertIn("超过读取上限", text)
        self.assertNotIn("third.txt", text)
        self.assertEqual([base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
                          for p in content if p["type"] == "image_url"], [photo])
        files = [p for p in event.message_obj.message if isinstance(p, File)]
        self.assertEqual([p.name for p in files], ["first.txt", "second.txt"])
        for p, expected in zip(files, (b"abc", b"de")):
            path = Path(await p.get_file())
            self.assertIn(str(path), text)
            self.assertEqual(path.read_bytes(), expected)

    async def test_managed_documents_share_window_timeout_within_album(self):
        target = self.controller.settings.target("tg-owner")
        await self.convert(document=dict(file_id="d", file_unique_id="d", file_name="first.txt"),
                           caption="keep caption", media_group_id="docs")
        event = self.platform.create_event(self.event.message_obj)
        event.created_at = self.clock.now().timestamp()
        event.message_obj.message.extend([File(name="second.txt", url="https://example.org/second"),
                                         Record(file="base64://YWJj")])
        settings = {"file_understanding_enabled": True, "file_understanding_timeout_seconds": 1}
        async def read(*args, **kwargs):
            await asyncio.sleep(0.65)
            return b"text"
        with patch.object(self.bridge, "file_settings", return_value=settings), patch(
                "astrbot_host.media.fetch_attachment", read):
            (await self.controller.session.prepare_core_reply(event, target)).unwrap()
        self.assertEqual([p.name for p in event.message_obj.message if isinstance(p, File)], ["first.txt"])
        self.assertIn("keep caption", event.message_str)
        self.assertIn("超时", event.message_str)
        self.assertIsInstance(event.message_obj.message[-1], Record)

    async def test_managed_records_and_files_share_the_read_budget(self):
        from companion_v2.execution.models import TaskError

        await self.convert(text="caption")
        parts = self.event.message_obj.message
        parts.extend([Record(file="base64://YWJj"), File(name="large.txt", url="https://example.org/large"),
                      File(name="small.txt", url="https://example.org/small"),
                      File(name="last.txt", url="https://example.org/last")])
        limits = []
        async def fetch(url, *, maximum_bytes):
            limits.append((url.rsplit("/", 1)[-1], maximum_bytes))
            if url.endswith("large"):
                raise TaskError("host_image_limit", "synthetic oversized file")
            return b"ok"

        with patch("companion_v2.conversation.media.models.MAX_TURN_BYTES", 5), patch(
                "astrbot_host.media.fetch_attachment", fetch):
            context, _ = await self.controller.session.synchronize(self.controller.settings.target("tg-owner"))
            self.bridge.file_settings = lambda *_: {"file_understanding_enabled": True}
            await self.bridge.prepare_telegram_album(self.event, context)
        self.assertEqual(limits, [("large", 2), ("small", 2)])
        caption, record, large, small, last = self.event.message_obj.message
        self.assertEqual((caption.text, record.file), ("caption", "base64://YWJj"))
        self.assertEqual((small.name, Path(await small.get_file()).read_bytes()), ("small.txt", b"ok"))
        self.assertEqual(large.text, last.text)
        self.assertIn("超过读取上限", last.text)

    async def test_captioned_album_preserves_photo_and_first_video_at_shared_limit(self):
        await self.check_mixed_album(oversized_first=False)

    async def test_mixed_album_keeps_later_videos_after_oversized_first(self):
        await self.check_mixed_album(oversized_first=True)

    async def test_uncaptioned_album_keeps_unavailable_quote_marker(self):
        from astrbot_host.media import load_native_image
        from tests.test_conversation_images import image_bytes

        self.bridge.image_type, self.bridge.image_loader = Image, load_native_image
        await self.convert(photo=[dict(file_id="photo", file_unique_id="p1", width=3, height=2)],
                           media_group_id="album", external_reply={"origin": {
                               "type": "hidden_user", "sender_user_name": "X", "date": 1700000000}})
        with patch("astrbot_host.media.fetch_image", AsyncMock(return_value=image_bytes("red"))):
            captured = (await self.bridge.capture(self.event)).unwrap()
        self.assertEqual(len(captured.images), 1)
        self.assertEqual(captured.text.count("引用消息不可用"), 1)

    async def test_uncaptioned_mixed_album_keeps_accepted_media(self):
        await self.check_mixed_album(oversized_first=False, caption=None)

    async def check_mixed_album(self, *, oversized_first, caption="mixed album"):
        import re
        from hashlib import sha256
        from astrbot_host.media import inspect_image, load_native_image
        from companion_v2.conversation.media.video.models import CapturedVideo, VideoDescriptor, VideoFrameDescriptor
        from tests.test_conversation_images import image_bytes

        photo = image_bytes("red")
        frame = inspect_image(image_bytes("green"), 0)
        originals = {f"https://example.org/v{n}.mp4": f"video-{n}".encode() for n in (1, 2, 3)}
        video_size = len(originals["https://example.org/v1.mp4"])
        count = 2 if oversized_first else 1
        video_limits = []

        async def video(locator, *, maximum_bytes):
            video_limits.append(maximum_bytes)
            if oversized_first and locator.position == 2:
                from companion_v2.execution.models import TaskError
                raise TaskError("host_video_limit", "oversized first video")
            video_bytes = originals[locator.locator]
            descriptor = VideoDescriptor(locator.position, "video/mp4", len(video_bytes),
                sha256(video_bytes).hexdigest(), 3, 2, 1, 1000, 0,
                (VideoFrameDescriptor(0, 0, frame.descriptor),), "decoder_elapsed_ms", "ffmpeg:external")
            return CapturedVideo(descriptor, video_bytes, (frame,))

        self.bridge.image_type, self.bridge.image_loader = Image, load_native_image
        self.bridge.video_type, self.bridge.video_loader = Video, video
        await self.convert(photo=[dict(file_id="photo", file_unique_id="p1", width=3, height=2)],
                           caption=caption, media_group_id="album")
        urls = list(originals)[:count + 1]
        self.event.message_obj.message.extend(Video(file=url) for url in urls)
        budget = len(photo) + count * (video_size + frame.descriptor.retained_bytes)
        with patch("astrbot_host.media.fetch_image", AsyncMock(return_value=photo)), patch(
                "companion_v2.conversation.media.models.MAX_TURN_BYTES", budget):
            captured = (await self.bridge.capture(self.event)).unwrap()
        unit = video_size + frame.descriptor.retained_bytes
        self.assertEqual(video_limits, [2 * unit, 2 * unit, unit] if oversized_first else [unit])
        self.assertEqual([item.descriptor.position for item in captured.images], [0])
        expected_positions = [3, 4] if oversized_first else [2]
        if caption is None:
            expected_positions = [position - 1 for position in expected_positions]
        self.assertEqual([item.descriptor.position for item in captured.videos], expected_positions)
        self.assertEqual(captured.text.count("附件超过读取上限"), 1)
        if caption:
            self.assertIn(caption, captured.text)

        # The SDK uses original video bytes for native providers, without decoding frames.
        async def fetch(url, *, maximum_bytes):
            if (oversized_first and url == urls[0]) or video_size > maximum_bytes:
                from companion_v2.execution.models import TaskError
                raise TaskError("host_image_limit", "synthetic transport byte limit")
            return originals[url]

        event = self.platform.create_event(self.event.message_obj)
        event.created_at = self.clock.now().timestamp()
        with patch("astrbot_host.media.fetch_image", AsyncMock(return_value=photo)), patch(
                "astrbot_host.video.loader.fetch_attachment", fetch), patch(
                "companion_v2.conversation.media.models.MAX_TURN_BYTES", len(photo) + count * video_size):
            await self.started_plugin().receive_companion_message(event)
        from astrbot.core.astr_main_agent import collect_initial_request
        with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
            request, _ = await collect_initial_request(event, NS(), NS(provider_wake_prefix="", provider_settings={}))
        payload = await request.assemble_context()
        text = "".join(part.get("text", "") for part in payload["content"])
        paths = re.findall(r"\[Video Attachment: name [^\]]*?, path ([^\]]+)\]", text)
        expected_urls = urls[1:] if oversized_first else urls[:1]
        self.assertEqual([Path(path).read_bytes() for path in paths], [originals[url] for url in expected_urls])
        if caption:
            self.assertIn(caption, text)
        self.assertIn("附件超过读取上限", text)
        self.assertEqual([base64.b64decode(part["image_url"]["url"].split(",")[1])
                          for part in payload["content"] if part["type"] == "image_url"], [photo])

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
        from tests.test_telegram_fork_platform import TelegramPlatform

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
        from tests.test_telegram_fork_platform import TelegramPlatform
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
        from tests.test_telegram_fork_platform import TelegramPlatform

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
