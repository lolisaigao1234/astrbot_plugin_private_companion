"""Group updates converted by AstrBot with offline, synthetic file contents."""

from dataclasses import replace
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
from astrbot_host.audio.loader import load_native_audio
from astrbot_host.files.loader import load_native_file
from astrbot_host.media import load_native_image
from astrbot_host.video.loader import load_native_video


class TelegramGroupMediaTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
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

    async def test_group_attachments_retain_the_speaking_member(self):
        async def download(url, *, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(audio_bytes())

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
              patch("astrbot_host.video.loader.fetch_attachment", AsyncMock(return_value=video_bytes())),
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
