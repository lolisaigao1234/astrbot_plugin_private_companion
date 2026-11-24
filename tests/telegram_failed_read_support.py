"""Synthetic HTTP bodies for Telegram capture and SDK preparation regressions."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from astrbot.core.message.components import File, Image, Record, Video


async def check_failed_reads(case, event, bridge, *, sdk=False):
    root = event.client.base_file_url + "/"
    event.message_obj.message.extend([
        Image(file=root + "header"), Image(file=root + "broken"),
        Record(file=root + "voice"), Video(file=root + "video"),
        File(name="note.txt", url=root + "file"),
    ])
    attempts = []
    consumed = []

    def get(url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        body = {"broken": b"bad", "voice": b"aa", "video": b"vvv", "file": b"ok"}.get(name, b"")
        position = 0
        async def read(limit):
            nonlocal position
            if position == 0:
                attempts.append((name, limit))
            if position == len(body) and name in ("voice", "video"):
                raise OSError("synthetic interrupted body")
            chunk = body[position:position + limit]
            consumed.append(len(chunk))
            position += len(chunk)
            return chunk
        request = MagicMock()
        request.__aenter__.return_value = SimpleNamespace(status=200, headers={},
            content_length=100 if name == "header" else None, content=SimpleNamespace(read=read))
        return request

    with patch("companion_v2.conversation.media.models.MAX_TURN_BYTES", 10), patch(
            "astrbot_host.media.aiohttp.ClientSession") as session, patch("astrbot_host.media.aiohttp.TCPConnector"):
        session.return_value.__aenter__.return_value.get = get
        if sdk:
            await bridge.prepare_telegram_album(event)
            parts = event.message_obj.message
            case.assertIn("caption", parts[0].text)
            case.assertEqual(parts[-1].name, "note.txt")
        else:
            captured = (await bridge.capture(event)).unwrap()
            case.assertIn("caption", captured.text)
            case.assertIn('"text": "ok"', captured.text)
    # The bounded reader permits one sentinel byte to detect overflow.
    case.assertEqual(attempts, [("broken", 11), ("voice", 8), ("video", 6), ("file", 3)])
    case.assertEqual(sum(consumed), 10)
