import unittest

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.tests.test_proactive_persona_consistency import (
    _ProactivePersonaHarness,
)

LONG_PERSONA = "<premise>前提</premise>\n" + "【身份】很长的设定。\n" * 600 + "<not_her>每条都带游戏比喻</not_her>"


class _VoiceHost:
    enable_persona_voice_channels = True
    reply_style_prompt = "回复长短跟着话题走。"
    persona_proactive_voice_prompt = "主动开口先接他正在做的事。"
    persona_conversation_voice_prompt = "【不像她的说法】每条都带游戏比喻。"

    _normalize_persona_voice_text = PrivateCompanionPlugin._normalize_persona_voice_text
    _format_persona_voice_channel_prompt_section = PrivateCompanionPlugin._format_persona_voice_channel_prompt_section
    _format_proactive_voice_prompt_sections = PrivateCompanionPlugin._format_proactive_voice_prompt_sections
    _format_proactive_voice_prompt = PrivateCompanionPlugin._format_proactive_voice_prompt


class _LongPersonaHarness(_ProactivePersonaHarness):
    async def _refresh_default_persona_prompt(self, umo=""):
        return LONG_PERSONA


class ProactiveVoiceSectionTests(unittest.IsolatedAsyncioTestCase):
    def test_proactive_voice_keeps_conversation_voice_when_both_are_set(self):
        rendered = _VoiceHost()._format_proactive_voice_prompt()
        self.assertIn("主动开口先接他正在做的事。", rendered)
        self.assertIn("每条都带游戏比喻", rendered)

    async def test_final_send_review_sees_end_of_long_persona(self):
        harness = _LongPersonaHarness()
        await harness._review_proactive_message_send_decision(
            {"umo": "s", "nickname": "小林", "user_id": "1"},
            "删到哪了。",
            reason="quiet_care",
            action="message",
            motive="陪一句",
            topic="代码",
        )
        self.assertIn("<not_her>每条都带游戏比喻</not_her>", harness.captured_prompt)


if __name__ == "__main__":
    unittest.main()
