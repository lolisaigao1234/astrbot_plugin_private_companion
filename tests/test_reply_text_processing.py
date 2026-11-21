from dataclasses import asdict, replace
import json
import time
import unittest

from companion_v2.configuration.models import ConfigurationError
from companion_v2.conversation.reply_preparation import prepare_reply
from companion_v2.conversation.reply_settings import validate
from companion_v2.conversation.segmentation import SegmentationPolicy, LLM_SEGMENT_MARKER as MARKER
from companion_v2.conversation.text_processing import (
    InternalExpressionProtocolFilter, ReplyTextPolicy, TEXT_DEFAULTS,
    strip_internal_expression_protocol,
)
from companion_v2.conversation.voice_reply import VoiceReplyPolicy


def policy(**changes):
    return ReplyTextPolicy(**{"content_cleanup_enabled": False, "content_cleanup_scope": "all", **changes})


class ReplyTextProcessingTests(unittest.TestCase):
    def test_default_word_segmentation_keeps_comma_linked_clauses_together(self):
        text = "知道呀～就是机器人内部给这条申请生成的唯一编号，方便后台批准或驳回时对上号。你不用管它，看昵称和问答就行。"
        self.assertEqual(SegmentationPolicy().parts(text), (
            "知道呀～就是机器人内部给这条申请生成的唯一编号，方便后台批准或驳回时对上号。",
            "你不用管它，看昵称和问答就行。"))

    def test_forbidden_word_guard_is_opt_in_scoped_and_case_aware(self):
        disabled = policy(recall_forbidden_words='["秘密"]')
        self.assertIsNone(disabled.forbidden_word_guard("这里有秘密"))
        group_only = policy(enable_forbidden_word_recall=True, recall_forbidden_words='["Secret"]',
                            recall_forbidden_scope="group_only")
        self.assertIsNone(group_only.forbidden_word_guard("secret", scope_kind="private"))
        self.assertEqual(group_only.forbidden_word_guard("secret", scope_kind="group"), "Secret")
        bot_only = policy(enable_forbidden_word_recall=True, recall_forbidden_words='["Secret"]',
                          recall_forbidden_scope="bot_only")
        self.assertIsNone(bot_only.forbidden_word_guard("secret", is_bot_output=False))
        self.assertEqual(bot_only.forbidden_word_guard("secret", is_bot_output=True), "Secret")
        bot_and_group = policy(enable_forbidden_word_recall=True, recall_forbidden_words='["Secret"]',
                               recall_forbidden_scope="bot_and_group")
        self.assertIsNone(bot_and_group.forbidden_word_guard("secret", scope_kind="private", is_bot_output=False))
        self.assertEqual(bot_and_group.forbidden_word_guard("secret", scope_kind="group", is_bot_output=False), "Secret")
        sensitive = policy(enable_forbidden_word_recall=True, recall_forbidden_words='["Secret"]',
                           recall_forbidden_word_case_sensitive=True)
        self.assertIsNone(sensitive.forbidden_word_guard("secret"))
        self.assertEqual(sensitive.forbidden_word_guard("Secret"), "Secret")

    def test_forbidden_word_configuration_is_registered_and_validated(self):
        fields = asdict(ReplyTextPolicy())
        fields.update({"enable_forbidden_word_recall": True, "recall_forbidden_words": '["spam"]',
                       "recall_forbidden_scope": "bot_and_group"})
        validate({**asdict(SegmentationPolicy()), "streaming": True, "part_gap_ms": 0,
                  **fields})
        with self.assertRaises(ConfigurationError):
            validate({**asdict(SegmentationPolicy()), "streaming": True, "part_gap_ms": 0,
                      **fields, "recall_forbidden_scope": "invalid"})

    def test_cleanup_is_enabled_by_default_while_replacement_stays_disabled(self):
        presentation = ReplyTextPolicy()
        self.assertTrue(presentation.content_cleanup_enabled)
        self.assertFalse(presentation.content_replacement_enabled)
        self.assertEqual(presentation.clean_parts(("原文。\n",)), ("原文。",))

    def test_configured_trailing_punctuation_cleanup_preserves_internal_punctuation(self):
        presentation = ReplyTextPolicy(content_cleanup_enabled=True, content_cleanup_scope="trailing",
                                       content_cleanup_words='["。", "，"]')
        self.assertEqual(presentation.content_cleanup_mode, "words")
        self.assertEqual(presentation.content_cleanup_scope, "trailing")
        self.assertEqual(json.loads(presentation.content_cleanup_words), ["。", "，"])
        self.assertTrue(presentation.content_cleanup_enabled)
        self.assertEqual(presentation.clean_parts(("甲\n乙。",)), ("甲\n乙",))
        self.assertEqual(presentation.clean_parts(("甲，乙。丙。， \n", "丁，戊，")),
                         ("甲，乙。丙", "丁，戊"))

    def test_cleanup_scope_accepts_multiple_positions_and_chinese_aliases(self):
        presentation = ReplyTextPolicy(content_cleanup_enabled=True,
                                       content_cleanup_scope="句首、句尾",
                                       content_cleanup_words=json.dumps(["。"], ensure_ascii=False))
        self.assertEqual(presentation.cleanup_scopes, frozenset({"leading", "trailing"}))
        self.assertEqual(presentation.clean_parts(("。开头。中间。结尾。",)),
                         ("开头。中间。结尾",))
        both_edges_and_middle = ReplyTextPolicy(
            content_cleanup_enabled=True, content_cleanup_scope="leading, middle, trailing",
            content_cleanup_words=json.dumps(["。", "，"], ensure_ascii=False))
        self.assertEqual(both_edges_and_middle.cleanup_scopes,
                         frozenset({"leading", "middle", "trailing"}))
        self.assertEqual(both_edges_and_middle.clean_parts(("。开头，中间。结尾。",)),
                         ("开头中间结尾",))
        with self.assertRaises(ValueError):
            ReplyTextPolicy(content_cleanup_enabled=True, content_cleanup_scope="句首、未知")

    def test_internal_reaction_expression_metadata_is_removed_before_segmentation(self):
        metadata = ('<pc_reaction_expression>{"purpose":"轻吐槽","emotion":"无奈",'
                    '"intensity":2}</pc_reaction_expression>')
        prepared = prepare_reply("在的在的，刚刚被签到局屏蔽过去了...不是不理你啦。\n" + metadata,
                                 SegmentationPolicy(enabled=False), policy())
        self.assertEqual(prepared.visible, "在的在的，刚刚被签到局屏蔽过去了...不是不理你啦。\n")
        self.assertEqual(prepared.parts, (prepared.visible.rstrip("\n"),))
        incomplete = prepare_reply("正文。<pc_reaction_expression>{\"emotion\":\"无奈\"",
                                   SegmentationPolicy(), policy())
        self.assertEqual(incomplete.parts, ("正文。",))
        escaped = prepare_reply("正文。&lt;pc_reaction_expression&gt;{&quot;emotion&quot;:&quot;无奈&quot;}"
                                "&lt;/pc_reaction_expression&gt;", SegmentationPolicy(), policy())
        self.assertEqual(escaped.parts, ("正文。",))
        hexadecimal = prepare_reply("正文。&#x3C;pc_reaction_expression&#x3E;{}"
                                    "&#x3C;/pc_reaction_expression&#x3E;", SegmentationPolicy(), policy())
        self.assertEqual(hexadecimal.parts, ("正文。",))

    def test_internal_reaction_expression_metadata_is_removed_across_stream_chunks(self):
        protocol = InternalExpressionProtocolFilter()
        visible = "".join((
            protocol.feed("正文。<pc_reaction_"),
            protocol.feed('expression\">{\"intent\":\"内部态度，害羞\"}'),
            protocol.feed("</pc_reaction_"),
            protocol.feed("expression>后续正文。"),
            protocol.finish(),
        ))
        self.assertEqual(visible, "正文。后续正文。")

    def test_provider_text_emit_call_is_unwrapped_instead_of_leaking(self):
        """网关把 newapi_emit_text 调用当普通文本返回时，保留正文、去掉外壳。"""
        self.assertEqual(
            strip_internal_expression_protocol(
                "<call:default_api:newapi_emit_text_v1>{content:当然是打游戏啦，玩什么都行。}</call>"),
            "当然是打游戏啦，玩什么都行。")
        self.assertEqual(
            strip_internal_expression_protocol("前缀<call:x:y>{content:中间}</call>后缀"),
            "前缀中间后缀")
        self.assertEqual(
            strip_internal_expression_protocol("<call:x:y>{content:只有开头"),
            "只有开头")
        # 不是发射调用时，普通提到的标签保持原样。
        self.assertEqual(strip_internal_expression_protocol("他说 <call:x> 是个标签"),
                         "他说 <call:x> 是个标签")

    def test_paired_control_blocks_are_removed_without_eating_delivery_sentinels(self):
        reaction = ('<pc_reaction_expression>{"purpose":"轻吐槽","emotion":"无奈",'
                    '"intensity":2}</pc_reaction_expression>')
        self.assertEqual(strip_internal_expression_protocol("正文。" + reaction + "后续"), "正文。后续")
        self.assertEqual(
            strip_internal_expression_protocol('好开心<pc_reaction_expression>{"x":1}[[PCTTS:abc123]]'),
            "好开心[[PCTTS:abc123]]")
        self.assertEqual(
            strip_internal_expression_protocol(
                '前半<pc_reaction_expression>{"x":1}<<PRIVATE_COMPANION_SPLIT>>后半'),
            "前半<<PRIVATE_COMPANION_SPLIT>>后半")
        self.assertEqual(
            strip_internal_expression_protocol(
                '好的<pc_reaction_expression>{"purpose":"用`表情`回应",'
                '"emotion":"开心","intensity":2}</pc_reaction_expression>'),
            "好的")

    def test_bare_reasoning_draft_is_removed_without_eating_visible_reply(self):
        draft = ("第一行正文\n"
                 "th用户说「今天好累」。\n"
                 "承认。\n"
                 "「候选一」\n"
                 "「候选二」\n"
                 "1-2句。\n"
                 "end实际正文\n"
                 "第二行正文")
        self.assertEqual(strip_internal_expression_protocol(draft),
                         "第一行正文\n实际正文\n第二行正文")
        # 无 th 前缀、但以“用户说/用户问”开头的草稿也要清理。
        self.assertEqual(strip_internal_expression_protocol(
            "回答。\n用户问「在吗」。\nend真正的回答"), "回答。\n真正的回答")
        # 普通英文、代码与 ended/endpoint 不得误删。
        for text in ("The end of the story", "end of story", "print('end')", "ended here",
                     "endpoint ready"):
            self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_word_ending_in_end_never_closes_a_bare_draft(self):
        # 以「开头的正文里，friend/Legend/send/weekend 中的 end 不是草稿结束标记。
        for text in ("「晚安」我先睡啦\n明天和friend\n一起去玩",
                     "「嗯」我在呢~ 刚刚看完一集\n下周末要不要一起去Legend展？",
                     "「早」\nsend",
                     "Sure!\n「Weekend」 plans: this weekend\nok?"):
            self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_visible_quoted_line_before_a_draft_is_kept(self):
        # 草稿从后面真正的 th 行开始，不能从前面可见的「行开始一路吞掉。
        self.assertEqual(strip_internal_expression_protocol(
            "「晚安。」\nth用户说今天很累。\nend明天见。"), "「晚安。」\n明天见。")

    def test_code_fence_after_quoted_line_is_kept(self):
        # 代码块里的 end 不是草稿结束标记，围栏不能被吞成半截。
        text = "「Ruby 示例」\n```ruby\ndef hi\n  puts 1\nend\n```\n好了"
        self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_draft_closed_by_end_after_cjk_sentence_punctuation_is_removed(self):
        # end 紧跟在。！？后面也是草稿结束标记。
        self.assertEqual(strip_internal_expression_protocol(
            "th用户问「在吗」。end我在。"), "我在。")
        self.assertEqual(strip_internal_expression_protocol(
            "第一行\nth用户说累。\n候选。1-2句。end实际正文"), "第一行\n实际正文")

    def test_th_draft_runs_through_a_later_user_said_line_to_its_own_end(self):
        # th 开头的草稿里出现“用户问”行，草稿第一行也不能漏出来。
        self.assertEqual(strip_internal_expression_protocol(
            "th先核对聊天记录。\n用户问还记得吗。\nend记得。"), "记得。")

    def test_each_draft_opener_keeps_its_own_guards(self):
        # th 草稿可以跨过任何行；用户说/用户问 草稿只在代码围栏处停下。
        for text, expected in (
                ("th先想。\nth再想。\nend好的", "好的"),
                ("用户说累了。\n用户问能不能休息。\nend好的", "好的"),
                ("th用户说今天很累。\n用户问明天见吗。\n候选：好。\nend明天见。", "明天见。")):
            with self.subTest(text=text):
                self.assertEqual(strip_internal_expression_protocol(text), expected)

    def test_inline_end_never_closes_a_quoted_reply(self):
        # 「开头的是可见回复，。！？后面的 end 不能把它当草稿删掉。
        # 「 正文里。！？后面的 end 也不能被跨过，否则会一路读到后面的行首 end，删掉中间的正文。
        for text in ("「早」今天学了个新单词。end 是结束的意思哦",
                     "「早」\n今天学了个新单词！end\n就是结束",
                     "「早」今天学了个新单词。end 是结束的意思哦\n再见\nend 也可以当动词"):
            with self.subTest(text=text):
                self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_a_draft_containing_a_backtick_line_is_left_visible(self):
        # 草稿里出现以 ``` 开头的行（代码块、行内代码行、四个反引号……）时，分不清哪个 end
        # 才是真的，整段原样保留：漏出草稿是 7.2.7 之前的现状，删掉可见正文才是新的伤害。
        for text in (
                # 以前会剥到草稿的 end 为止，现在整段保留。
                "th先写个示例。\n```ruby\ndef hi\nend\n```\n再想想。\nend回答",
                "th先写个示例。\r\n```ruby\r\ndef hi\r\nend\r\n```\r\n再想想。\r\nend回答",
                "th先举例。\n```ruby\nputs 1\n````\nend代码如下：\n```\ndef f\nend\n```\n完",
                "th先写。\n```text\n```example\nend\n```\nend回答",
                "好的。\nth先装依赖：\n```gem install x```\n想好了。\nend代码如下：\n```\ndef f\nend\n```\n完",
                "好的。\nth先装依赖：\n```pip install x``` 然后运行\n想好了。\nend代码如下。",
                "好的。\nth先装依赖：\n```pip install x```。\n想好了。\nend代码如下。",
                "好的。\nth先装依赖：\n```a``` 和 ```b```\n想好了。\nend代码如下。",
                # 第 9 轮：行内代码后面的 。end、跨到下一行的第二段行内代码。
                "th先想。\n```ls``` 就行。end请用 ls 查看。\nend 是结束标记。",
                "th先举例。\n```a``` 和 ```第一行\nend\n第二行```\n再想想。\nend回答",
                # 一直都原样保留的。
                "th先写个示例。\n```ruby\ndef hi\nend\n再想想。\nend回答",
                "回复\nth思考没有结尾\n正文\n```ruby\nif x\nend\n```",
                "回复\nth思考没有结尾\n正文\n````ruby\nif x\nend\n````\n完",
                "回复\nth思考没有结尾\n正文\n```ruby `x`\nif x\nend\n```\n完",
                "th我写：\n````ruby\ndef f\nend\n````\nend回答",
                "th先想想。\n```先运行 `ls`，再看\nend\n结果```\n这是正文",
                "用户说要示例。\n```py\nx\n```\nend回答",
                "「示例」\n```py\nx\n```\nend回答"):
            with self.subTest(text=text):
                self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_a_line_start_end_that_does_not_close_the_draft_is_a_boundary(self):
        # 行首的 end 行如果不是这段草稿的结束标记，就分不清后面哪个 end 才是真的，
        # 草稿不能跨过它，整段原样保留，不能删掉中间的正文。
        for text in ("「候选」\n想想。\nend\n正文第一段。\nend 表示结束。",
                     "th想。\nend of story\n正文。\nend回答"):
            with self.subTest(text=text):
                self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_nothing_is_stripped_inside_or_after_a_rejected_explicit_draft(self):
        # th/用户 草稿被边界拒掉以后，不能在它里面或后面另起一段「/用户 匹配，删掉可见回答。
        for text in ("th先想。\nend of story\n「候选」\n想好了。end请保留这句。\nend 是结束标记。",
                     "th先想。\n```text\nfoo\n```\n「候选」\n想好了。end请保留这句。\nend 是结束标记。",
                     "th先想。\nend of story\n「候选」\n试试。end正文第一段。\n正文第二段。\nend 表示结束。",
                     "th先想。\nend of story\n用户说累了。\nend请保留这句。",
                     "th想想。\n```x```\n「安装」说完了。end好的，装好了！\n今天学了个单词：\nend 是结束的意思",
                     "th想。\nend of story\n「好」我想。end正文。\nend回答"):
            with self.subTest(text=text):
                self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_quoted_heading_followed_by_a_bare_end_line_is_kept(self):
        # 「开头的可能是可见回复：单独一行的 end 后面没有中文，不是草稿结束标记。
        for text in ("「英语词汇」\nend\n这个词既能作名词也能作动词。",
                     "「英语词汇」\r\nend\r\n这个词既能作名词也能作动词。"):
            with self.subTest(text=text):
                self.assertEqual(strip_internal_expression_protocol(text), text)

    def test_end_followed_by_crlf_closes_user_said_and_quoted_drafts(self):
        # CRLF 下 用户问 草稿在 end 处结束，end 后面的 \r\n 原样保留。
        self.assertEqual(strip_internal_expression_protocol(
            "回答。\r\n用户问「在吗」。\r\nend\r\n真正的回答"), "回答。\r\n\r\n真正的回答")
        # 「 草稿在 CRLF 下仍在后面紧跟中文的行首 end 处结束。
        self.assertEqual(strip_internal_expression_protocol(
            "「候选」\r\n想想。\r\nend真正的回答"), "真正的回答")

    def test_end_followed_by_crlf_closes_a_th_draft(self):
        # end 后面是 \r\n 也算草稿结束；草稿只删到 end，后面的换行和正文原样保留，
        # 和 LF 时一致（"th想。\nend\n回答" 得到 "\n回答"）。
        self.assertEqual(strip_internal_expression_protocol(
            "th想一下。\r\nend\r\n第一段讲解。end 是结束标记。"), "\r\n第一段讲解。end 是结束标记。")

    def test_th_draft_with_many_fences_and_no_end_finishes_quickly(self):
        # 没有 end 的 th 草稿里有很多代码块时，正则不能指数级回溯卡住事件循环。
        cases = {
            "trailing spaces after each close":
                "th想一下。\n" + "```py\nx.append(1)\n```      \n正文\n" * 40 + "结尾",
            "one-line snippet shifts the pairing":
                "th用户想要几个例子，我整理一下\n先装依赖：\n```pip install requests```\n"
                + "".join("例子%d：\n```python\nitems.append(%d)\n```\n" % (i, i)
                          for i in range(7)) + "就这些。",
            "text after each closing fence":
                "th让我想想\n" + "".join("```python\nitems.append(%d)\n```  说明\n" % i
                                       for i in range(8)) + "```\n截断",
            "bare fence mentioned in the draft":
                "好的。\nth用户要几段示例，每段用\n```\n包起来，逐段解释。\n下面是示例：\n"
                + "```python\nitems.append(0)\n```\n第0段说明。\n" * 8,
            "CRLF with trailing spaces after each close":
                "th想一下。\r\n" + "```py\r\nx.append(1)\r\n```      \r\n正文\r\n" * 40 + "结尾",
            "four-backtick fences":
                "th想一下。\n" + "````py\nx.append(1)\n````      \n正文\n" * 40 + "结尾",
            "many 用户 openers before a boundary":
                "用户说。\n" * 4000 + "end of story\nend回答",
        }
        for name, text in cases.items():
            with self.subTest(name):
                start = time.perf_counter()
                self.assertEqual(strip_internal_expression_protocol(text), text)
                self.assertLess(time.perf_counter() - start, 0.5)

    def test_control_block_cleanup_handles_escaping_fullwidth_orphans_and_unknown_names(self):
        self.assertEqual(strip_internal_expression_protocol(
            "正文。&lt;pc_reaction_expression&gt;{}&lt;/pc_reaction_expression&gt;"), "正文。")
        self.assertEqual(strip_internal_expression_protocol(
            "正文。&#x3C;pc_reaction_expression&#x3E;{}"
            "&#x3C;/pc_reaction_expression&#x3E;"), "正文。")
        self.assertEqual(strip_internal_expression_protocol(
            "正文。＜pc_reaction_expression＞{}＜/pc_reaction_expression＞后续"), "正文。后续")
        self.assertEqual(strip_internal_expression_protocol(
            "正文。</pc_reaction_expression>"), "正文。")
        self.assertEqual(strip_internal_expression_protocol(
            "<pc_member_safety>{\"safe\":true}</pc_member_safety>"
            "<personality_sync>{\"ok\":true}</personality_sync>"
            "<pc_unregistered>{\"x\":1}</pc_unregistered>正文"), "正文")

    def test_unparsed_tool_call_envelopes_never_become_visible_text(self):
        # A provider that returns the model's tool call as completion text must
        # not post the wire protocol into the group.  This is the exact shape a
        # group turn produced: choose(mode=skip) written out instead of called.
        envelope = ("<｜｜DSML｜｜ calls>\n"
                    '<｜｜DSML｜｜ invoke name="group_participation_choose">\n'
                    '<｜｜DSML｜｜ parameter name="mode" string="true">skip</｜｜DSML｜｜ parameter>\n'
                    '<｜｜DSML｜｜ parameter name="reason" string="true">对方在跟开发者聊注册和iOS登录的进度'
                    "</｜｜DSML｜｜ parameter>\n"
                    "</｜｜DSML｜｜ invoke>\n"
                    "</｜｜DSML｜｜ calls>")
        self.assertEqual(strip_internal_expression_protocol(envelope), "")
        self.assertEqual(strip_internal_expression_protocol("我先看看。\n" + envelope), "我先看看。")
        self.assertEqual(strip_internal_expression_protocol(envelope + "\n先这样"), "先这样")
        # An envelope the provider cut off must not leave its tail behind either.
        self.assertEqual(strip_internal_expression_protocol("正文" + envelope.split("</")[0]), "正文")
        self.assertEqual(strip_internal_expression_protocol(
            '<|DSML|tool_calls><|DSML|invoke name="x">{}</|DSML|invoke></|DSML|tool_calls>'), "")

    def test_tool_call_protocol_cleanup_keeps_prose_and_code_examples(self):
        marker = "<｜｜DSML｜｜ calls>"
        self.assertEqual(strip_internal_expression_protocol("正文" + marker), "正文")
        self.assertEqual(strip_internal_expression_protocol("DSML 是模型协议"), "DSML 是模型协议")
        code = "示例：\n```\n" + marker + "\n```\n保留"
        self.assertEqual(strip_internal_expression_protocol(code), code)

    def test_control_block_cleanup_preserves_code_and_tts_but_not_prose_after_a_partial_tag(self):
        code = '示例：`<pc_reaction_expression>{"x":1}</pc_reaction_expression>` 保留'
        self.assertEqual(strip_internal_expression_protocol(code), code)
        self.assertEqual(strip_internal_expression_protocol(
            "<pc_tts>朗读</pc_tts>正文"), "<pc_tts>朗读</pc_tts>正文")
        self.assertEqual(strip_internal_expression_protocol(
            "你可以在 <thinking> 里放草稿，但不要外发。这句话很重要。"),
            "你可以在 <thinking> 里放草稿，但不要外发。这句话很重要。")

    def test_stream_filter_finish_preserves_sentinels_trapped_in_an_unclosed_block(self):
        protocol = InternalExpressionProtocolFilter()
        visible = protocol.feed('正文<pc_reaction_expression>{"x":1}[[PCTTS:abc123]]')
        self.assertEqual(visible + protocol.finish(), "正文[[PCTTS:abc123]]")

    def test_voice_wrappers_are_not_visible_when_voice_is_disabled(self):
        prepared = prepare_reply("<pc_tts>前置。</pc_tts>普通正文。", SegmentationPolicy(), policy())
        self.assertEqual(prepared.parts, ("前置。普通正文。",))
        self.assertNotIn("pc_tts", prepared.visible)
        encoded = prepare_reply("&#x3C;pc_tts&#x3E;正文。&#x3C;/pc_tts&#x3E;", SegmentationPolicy(), policy())
        self.assertEqual(encoded.parts, ("正文。",))

    def test_voice_tag_variants_remain_protected_from_content_replacement(self):
        presentation = policy(content_replacement_enabled=True,
                              content_replacements='["preserve => changed"]')
        source = "&lt;PC-TTS lang='ja'&gt;preserve\nstill spoken.</PCC_TTS>"
        self.assertEqual(presentation.replace_text(source), source)

    def test_ordered_replacements_precede_rule_length_and_support_tokens(self):
        presentation = policy(content_replacement_enabled=True,
            content_replacements=json.dumps(["hi => 你好呀", {"from": "呀", "to": "哇"},
                                              "<tab> => <space>", "删 => <empty>"]))
        segmentation = SegmentationPolicy(enabled=True, rules_enabled=True,
            threshold=500, min_segment_chars=1, max_segments=3)
        result = prepare_reply("hi。hi。\t删", segmentation, presentation)
        self.assertEqual(result.parts, ("你好哇。", "你好哇。 "))
        self.assertNotIn("格式数据", presentation.instruction)

    def test_protected_markdown_nested_quotes_urls_and_declared_facts_are_literal(self):
        protected = ('（外层 hi（内层 hi））', '“hi，原句”', 'https://host.test/hi?q=hi',
                     '```text\nhi\n```', '[hi](https://host.test/hi)', '<pc_tts>hi</pc_tts>',
                     "hi 是事实。")
        text = "hi\n\n" + "\n\n".join(protected)
        presentation = policy(content_replacement_enabled=True,
                              content_replacements='[{"from":"hi","to":"嗨"}]')
        replaced = presentation.replace_text(text, statements=(protected[-1],))
        self.assertTrue(replaced.startswith("嗨\n\n"))
        for literal in protected:
            self.assertIn(literal, replaced)

    def test_inline_media_and_url_do_not_protect_adjacent_prose(self):
        presentation = policy(content_replacement_enabled=True, content_replacements='["hi => 嗨"]')
        text = 'hi <pc_tts>hi</pc_tts> hi https://host.test/hi_name hi'
        self.assertEqual(presentation.replace_text(text),
                         '嗨 <pc_tts>hi</pc_tts> 嗨 https://host.test/hi_name 嗨')
        self.assertEqual(presentation.replace_text('**hi <pc_tts>hi</pc_tts> hi**'),
                         '**hi <pc_tts>hi</pc_tts> hi**')

    def test_cleanup_all_and_trailing_words_preserve_protected_spans_across_parts(self):
        presentation = policy(content_cleanup_enabled=True, content_cleanup_words='["。", "，"]')
        self.assertEqual(presentation.clean_parts(("甲。乙，", "（丙。", "丁，）戊。")),
                         ("甲乙", "（丙。", "丁，）戊"))
        middle = replace(presentation, content_cleanup_scope="middle")
        self.assertEqual(middle.clean_parts(("甲。乙，", "（丙。", "丁，）戊。")),
                         ("甲乙，", "（丙。", "丁，）戊。"))
        leading = replace(presentation, content_cleanup_scope="leading")
        self.assertEqual(leading.clean_parts(("。甲。乙", "，丙。", "丁，）戊。")),
                         ("甲。乙", "丙。", "丁，）戊。"))
        trailing = replace(presentation, content_cleanup_scope="trailing")
        self.assertEqual(trailing.clean_parts(("甲。乙。， \n", "丙。（丁。）", "戊。（己。）。，")),
                         ("甲。乙", "丙。（丁。）", "戊。（己。）"))
        facts = ("甲。", "乙。")
        self.assertEqual(presentation.clean_parts(facts, statements=("甲。乙。",)), facts)

    def test_words_cleanup_handles_fullwidth_and_halfwidth_entries(self):
        presentation = policy(content_cleanup_enabled=True, content_cleanup_scope="trailing",
                              content_cleanup_words=json.dumps(["。", "."]))
        self.assertEqual(presentation.clean_parts(("甲。乙。", "丙.丁.")), ("甲。乙", "丙.丁"))

    def test_empty_word_list_falls_back_to_the_configured_regex_rule(self):
        presentation = policy(content_cleanup_enabled=True, content_cleanup_scope="trailing",
                              content_cleanup_words="[]", content_cleanup_rule=r"[。.．\s]")
        self.assertEqual(presentation.clean_parts(("甲。乙。 \n", "丙.丁．")), ("甲。乙", "丙.丁"))

    def test_empty_word_list_applies_the_regex_rule_to_every_position(self):
        presentation = policy(content_cleanup_enabled=True, content_cleanup_scope="all",
                              content_cleanup_words="[]", content_cleanup_rule=r"[。.]")
        self.assertEqual(presentation.clean_parts(("甲。乙.丙",)), ("甲乙丙",))

    def test_regex_cleanup_zero_width_and_execution_budget(self):
        presentation = policy(content_cleanup_enabled=True, content_cleanup_mode="regex",
                              content_cleanup_rule=r"[。!]+")
        self.assertEqual(presentation.clean_parts(("甲。乙!!（丙!）",)), ("甲乙（丙!）",))
        self.assertEqual(replace(presentation, content_cleanup_scope="trailing").clean_parts(("甲。乙!!",)), ("甲。乙",))
        self.assertEqual(replace(presentation, content_cleanup_rule=r"(?=$)",
                                content_cleanup_scope="trailing").clean_parts(("甲。",)), ("甲。",))
        with self.assertRaises(ValueError):
            replace(presentation, content_cleanup_rule=r"(a|aa)+$").clean_parts(("a" * 2000 + "!",))

    def test_empty_parts_are_removed_without_moving_authored_speech_to_another_part(self):
        presentation = policy(content_cleanup_enabled=True, content_cleanup_scope="leading",
                              content_cleanup_words='["删", "\\n"]',
                              content_replacement_enabled=True,
                              content_replacements='["说 => 改", "语 => 换"]')
        segmentation = SegmentationPolicy(enabled=True, llm_controlled=True)
        voice = VoiceReplyPolicy(generation_mode="model_tag", segment_mode="first")
        text = f"删\n{MARKER}\n<pc_tts>语。</pc_tts>说\n{MARKER}\n后文"
        prepared = prepare_reply(text, segmentation, presentation, voice)
        self.assertEqual(prepared.parts, ("语。改", "后文"))
        self.assertEqual(prepared.kept_indices, (1, 2))
        self.assertEqual(prepared.speech(voice), (("语。", "planned"), ("", "segment_skipped")))
        derived = replace(voice, generation_mode="plain", segment_mode="each")
        self.assertEqual([entry[0] for entry in prepared.speech(derived)], list(prepared.parts))
        with self.assertRaises(ValueError):
            prepare_reply("删", segmentation, presentation)

    def test_reserved_markers_survive_replacements_until_normal_segmentation(self):
        presentation = policy(content_replacement_enabled=True,
                              content_replacements='["PRIVATE => OTHER", "甲 => 嗨"]')
        prepared = prepare_reply(f"甲\n{MARKER}\n乙", SegmentationPolicy(enabled=True, llm_controlled=True), presentation)
        self.assertEqual(prepared.parts, ("嗨", "乙"))

    def test_settings_are_complete_or_legacy_and_invalid_rules_fail_before_save(self):
        legacy = {**asdict(SegmentationPolicy()), "streaming": False, "part_gap_ms": 0}
        self.assertEqual(validate(legacy), SegmentationPolicy())
        self.assertEqual(validate({**legacy, **TEXT_DEFAULTS}), SegmentationPolicy())
        with self.assertRaises(ConfigurationError):
            validate({**legacy, "content_cleanup_enabled": True})
        invalid = ({"content_cleanup_enabled": 1}, {"content_cleanup_rule": "["},
                   {"content_cleanup_scope": "head"}, {"content_cleanup_words": '[""]'},
                   {"content_replacements": '[{"from":"a","from":"b","to":"c"}]'},
                   {"content_replacements": '[{"from":"","to":"x"}]'},
                   {"content_replacements": '["broken"]'})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                validate({**legacy, **TEXT_DEFAULTS, **changes})
        expansion = policy(content_replacement_enabled=True,
                           content_replacements=json.dumps([{"from": "a", "to": "b" * 500}]))
        with self.assertRaises(ValueError):
            expansion.replace_text("a" * 200)

    def test_host_and_provider_control_tag_families_are_registered(self):
        # The host injects <system_reminder> blocks itself
        # (astrbot/core/astr_main_agent.py:1033, and
        # astrbot/builtin_stars/astrbot/group_chat_context.py:33-39), and AstrBot
        # names <think> beside <thinking> as a real provider spelling
        # (astrbot/core/provider/sources/openai_source.py:862 comment, :864 regex).
        #
        # <reasoning> is deliberately NOT covered and NOT asserted here: nothing in
        # AstrBot 4.29.1 produces it.  There is no such literal anywhere in the
        # tree, and Gemini reads reasoning from the structured ``part.thought``
        # flag instead of a text tag (gemini_source.py:527-529).  Registering it
        # would be speculation.
        for spelled in (
            "<system_reminder>群聊上下文</system_reminder>正文",
            "正文<system_reminder>群聊上下文</system_reminder>",
            "&lt;system_reminder&gt;x&lt;/system_reminder&gt;正文",
            "＜system_reminder＞x＜/system_reminder＞正文",
            "<think>hmm</think>正文",
            "正文</think>",
        ):
            with self.subTest(spelled=spelled):
                self.assertEqual(strip_internal_expression_protocol(spelled), "正文")

        # Registering the family must not borrow the truncate authorisation the
        # prompt-only blocks have: an unclosed tail is prose the model still wrote.
        self.assertEqual(
            strip_internal_expression_protocol("正文<think>还没想完"),
            "正文<think>还没想完")

        # Code examples and delivery sentinels keep their existing exemptions.
        self.assertEqual(
            strip_internal_expression_protocol("看这个`<think>示例</think>`就行"),
            "看这个`<think>示例</think>`就行")
        self.assertEqual(
            strip_internal_expression_protocol("正文<think>x</think>[[PCTTS:abc123]]"),
            "正文[[PCTTS:abc123]]")

        # The long spelling that was already registered keeps working unchanged.
        self.assertEqual(
            strip_internal_expression_protocol("<thinking>hmm</thinking>正文"), "正文")


if __name__ == "__main__":
    unittest.main()
