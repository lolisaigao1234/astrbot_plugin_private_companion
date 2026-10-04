# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from astrbot_plugin_private_companion.private_image import PrivateImageMixin


class _ImageHarness(PrivateImageMixin):
    def __init__(self, root: Path) -> None:
        self.data_dir = str(root)
        self.context_image_caption_timeout_seconds = 8
        self.private_image_provider_timeout_seconds = 12
        self.private_image_vision_wait_seconds = 30
        self.enable_private_image_self_recognition = True


class PrivateImageLocalSourceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_vision_master_switch_disables_context_captioning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = _ImageHarness(Path(temporary))
            harness.enable_private_image_self_recognition = False
            calls = 0

            async def transcribe(_sources, **_kwargs):
                nonlocal calls
                calls += 1
                return "不应调用"

            harness._transcribe_private_inbound_images = transcribe
            result = await harness._caption_context_image_sources(["source"])

            self.assertEqual("", result)
            self.assertEqual(0, calls)

    async def test_private_vision_master_switch_stops_transcription_before_preparing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = _ImageHarness(Path(temporary))
            harness.enable_private_image_self_recognition = False

            async def prepare(_sources, **_kwargs):
                raise AssertionError("关闭总开关后不应预处理图片")

            harness._prepare_private_image_sources_for_model = prepare
            result = await harness._transcribe_private_inbound_images(["missing.jpg"])

            self.assertEqual("", result)

    async def test_failed_context_image_caption_is_cooled_down(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = _ImageHarness(Path(temporary))
            calls = 0

            async def transcribe(_sources, **_kwargs):
                nonlocal calls
                calls += 1
                return ""

            harness._transcribe_private_inbound_images = transcribe
            first = await harness._caption_context_image_sources(["missing.jpg"])
            second = await harness._caption_context_image_sources(["missing.jpg"])

            self.assertEqual("", first)
            self.assertEqual("", second)
            self.assertEqual(1, calls)

    async def test_windows_file_uri_is_normalized_before_model_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "带 空格.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            harness = _ImageHarness(root)
            uri = image.resolve().as_uri()

            prepared = await harness._prepare_private_image_sources_for_model([uri], namespace="private_vision")

            self.assertEqual([str(image.resolve())], prepared)
            self.assertTrue(harness._private_image_source_to_model_url(uri).startswith("data:image/png;base64,"))

    async def test_inline_image_sources_bypass_local_path_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = _ImageHarness(Path(temporary))
            self.assertIsNone(harness._private_image_local_path_from_source("https://example.com/image"))
            harness._private_image_local_path_from_source = lambda _source: self.fail(
                "inline image sources must not be interpreted as filesystem paths"
            )

            prepared = await harness._prepare_private_image_sources_for_model(
                ["base64://AAAA", "data:image/png;base64,BBBB"]
            )

            self.assertEqual(
                ["base64://AAAA", "data:image/png;base64,BBBB"], prepared
            )

    async def test_unreadable_path_probe_does_not_hide_later_valid_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = _ImageHarness(Path(temporary))
            broken = Mock()
            broken.exists.side_effect = OSError("path probe failed")
            valid = Mock()
            valid.exists.return_value = True
            valid.is_file.return_value = True
            valid.resolve.return_value = Path(temporary) / "valid.png"
            harness._private_image_local_path_from_source = Mock(side_effect=[broken, valid])
            harness._private_image_local_path_is_allowed = Mock(return_value=True)

            refs = harness._private_image_sources_for_astrbot_request(["broken", "valid"])

            self.assertEqual([str(valid.resolve.return_value)], refs)

    async def test_context_caption_waits_for_visual_budget_not_legacy_eight_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = _ImageHarness(Path(temporary))
            observed: dict[str, float] = {}

            async def transcribe(_sources, **_kwargs):
                return "图片类型：照片"

            async def fake_wait_for(awaitable, timeout):
                observed["timeout"] = timeout
                return await awaitable

            harness._transcribe_private_inbound_images = transcribe
            with patch("astrbot_plugin_private_companion.private_image.asyncio.wait_for", new=fake_wait_for):
                result = await harness._caption_context_image_sources(["source"])

            self.assertEqual("图片类型：照片", result)
            self.assertEqual(30, observed["timeout"])


if __name__ == "__main__":
    unittest.main()
