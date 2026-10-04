from __future__ import annotations

import pytest

from astrbot_plugin_private_companion.context_editor_core import (
    EditorValidationError,
    apply_submitted_edits,
    editor_messages,
    history_revision,
    strip_thinking_parts,
)


def test_editor_messages_preserve_every_source_message() -> None:
    history = [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": [{"type": "text", "text": "第二条"}]},
    ]

    rows = editor_messages(history)

    assert [row["sourceIndex"] for row in rows] == [0, 1]
    assert [row["message"] for row in rows] == history
    assert all(row["sourceHash"] for row in rows)


def test_apply_submitted_edits_rejects_role_changes() -> None:
    history = [{"role": "assistant", "content": "原消息"}]
    row = editor_messages(history)[0]
    row["message"] = {"role": "user", "content": "篡改角色"}

    with pytest.raises(EditorValidationError, match="ROLE"):
        apply_submitted_edits(history, [row])


def test_revision_changes_when_message_text_changes() -> None:
    before = [{"role": "assistant", "content": "旧文本"}]
    after = [{"role": "assistant", "content": "新文本"}]

    assert history_revision(before) != history_revision(after)


def test_thinking_is_removed_only_from_request_copy() -> None:
    history = [{
        "role": "assistant",
        "content": [
            {"type": "think", "thinking": "内部推理"},
            {"type": "text", "text": "对用户可见"},
        ],
    }]

    sanitized = strip_thinking_parts(history)

    assert sanitized == [{
        "role": "assistant",
        "content": [{"type": "text", "text": "对用户可见"}],
    }]
    assert len(history[0]["content"]) == 2
