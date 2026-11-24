"""Exercise quoted photos through AstrBot's Telegram converter and provider serializer."""

import base64
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from astrbot.core.astr_main_agent import MainAgentBuildConfig, _process_quote_message, collect_initial_request
from astrbot.core.message.components import Image, Reply
from telegram import PhotoSize
from tests.test_conversation_images import image_bytes


async def check_provider_photo_budget(case, make_event, target_id, sender):
    direct = [image_bytes((i * 20, 5, 5)) for i in range(1, 9)]
    quoted = image_bytes("blue")
    photo = dict(file_id="direct", file_unique_id="d", width=3, height=2)
    original = dict(photo=[dict(photo, file_id="quoted")], caption="original caption")

    async def get_file(size, *args, **kwargs):
        data = direct[0] if size.file_id == "direct" else quoted
        return NS(file_path="base64://" + base64.b64encode(data).decode())

    target = case.controller.settings.target(target_id)
    bridge = case.controller.session.bridge
    for count, spare, keep in ((8, len(quoted), False), (2, 1, False),
                               (1, 0, False), (1, len(quoted), True)):
        with case.subTest(count=count, spare=spare):
            fields = dict(photo=[photo], caption="current caption")
            if count > 1:
                fields["media_group_id"] = "album"
            with patch.object(PhotoSize, "get_file", get_file):
                event = await make_event(fields, original)
            nickname = next(part.sender_nickname for part in event.message_obj.message if isinstance(part, Reply))
            event.message_obj.message.extend(Image.fromBytes(data) for data in direct[1:count])
            budget = sum(map(len, direct[:count])) + spare
            loader = AsyncMock(wraps=bridge.image_loader)
            with patch.object(bridge, "image_loader", loader), patch(
                    "companion_v2.conversation.media.models.MAX_TURN_BYTES", budget):
                prepared = (await case.controller.session.prepare_core_reply(event, target)).unwrap()
            quote, = prepared["native_input"].quotes
            case.assertEqual(quote.sender_id, str(sender))
            case.assertIn("original caption", quote.text)
            with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
                request, ref = await collect_initial_request(event, NS(), MainAgentBuildConfig(tool_call_timeout=30))
                await _process_quote_message(event, request, "", NS(), image_ref=ref)
                payload = await request.assemble_context()
            images = [base64.b64decode(part["image_url"]["url"].split(",")[1])
                      for part in payload["content"] if part["type"] == "image_url"]
            case.assertEqual(images, direct[:count] + ([quoted] if keep else []))
            text = "\n".join(part["text"] for part in payload["content"] if part["type"] == "text")
            case.assertIn("current caption", text)
            case.assertIn("original caption", text)
            case.assertIn(f"({nickname}):", text)
            limits = [budget - sum(map(len, direct[:i])) for i in range(count)]
            if count < 8 and spare:
                limits.append(spare)
            case.assertEqual([call.kwargs["maximum_bytes"] for call in loader.await_args_list], limits)


async def check_provider_quoted_attachments(case, make_event, target_id):
    from pathlib import Path
    from astrbot.core.message.components import File, Plain, Video
    from companion_v2.execution.models import TaskError
    from telegram import Document, Video as TelegramVideo

    document = dict(file_id="d", file_unique_id="d", file_name="notes.txt")
    video = dict(file_id="v", file_unique_id="v", width=2, height=2, duration=1)
    bridge = case.controller.session.bridge
    target = case.controller.settings.target(target_id)
    bot = case.platform.client
    bot.base_file_url = f"https://api.telegram.org/file/bot{bot.token}"
    bot.get_file = AsyncMock(return_value=NS(file_path=bot.base_file_url + "/quoted-media"))
    payload = b"attachment"
    for kind, enabled, budget, keep in (("document", False, 32, False), ("document", True, 10, True),
                                        ("video", False, 32, False), ("video", True, 3, False), ("video", True, 10, True)):
        with case.subTest(kind=kind, enabled=enabled, budget=budget):
            location = NS(file_path=case.platform.client.base_file_url + "/quoted-media")
            original = {kind: document if kind == "document" else video, "caption": "quoted caption"}
            with patch.object(Document if kind == "document" else TelegramVideo, "get_file",
                              AsyncMock(return_value=location)):
                event = await make_event({"text": "question about original"}, original)

            async def read(url, *, maximum_bytes):
                case.assertEqual(maximum_bytes, budget)
                if len(payload) > maximum_bytes:
                    raise TaskError("host_image_limit", "too large")
                return payload

            fetch = AsyncMock(side_effect=read)
            with patch.object(bridge, "file_settings", return_value={"file_understanding_enabled": enabled,
                                                                 "video_understanding_quoted_enabled": enabled}), patch(
                    "companion_v2.conversation.media.models.MAX_TURN_BYTES", budget), patch(
                    "astrbot_host.media.fetch_attachment", fetch), patch("astrbot_host.video.loader.fetch_attachment", fetch):
                prepared = (await case.controller.session.prepare_core_reply(event, target)).unwrap()
            case.assertIn("quoted caption", prepared["native_input"].quotes[0].text)
            reply = next(p for p in event.message_obj.message if isinstance(p, Reply))
            case.assertEqual(fetch.await_count, int(enabled))
            if keep and kind == "document":
                file = next(c for c in reply.chain if isinstance(c, File))
                case.assertEqual(Path(await file.get_file()).read_bytes(), payload)
            elif keep:
                clip = next(c for c in reply.chain if isinstance(c, Video))
                case.assertEqual(base64.b64decode(clip.file.removeprefix("base64://")), payload)
            else:
                case.assertFalse(any(isinstance(c, (File, Video)) for c in reply.chain))
                case.assertTrue(any(isinstance(c, Plain) for c in reply.chain))
            with patch("astrbot.core.astr_main_agent._get_session_conv", AsyncMock(return_value=NS(history="[]"))):
                req, ref = await collect_initial_request(event, NS(), MainAgentBuildConfig(tool_call_timeout=30))
                await _process_quote_message(event, req, "", NS(), image_ref=ref)
                serialized = str(await req.assemble_context())
            case.assertIn("question about original", serialized)
            case.assertNotIn(case.platform.client.token, serialized)
