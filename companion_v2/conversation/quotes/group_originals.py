"""Link group quotes to known members and confirmed Bot replies without private projection."""

from dataclasses import replace
import json

from companion_v2.execution.models import TaskContext, TaskError, digest


def original_sources(messages, context, event, quote, text, at):
    if quote.message_id == event.event_id:
        raise TaskError("quote_original_cycle", "an input cannot quote itself")
    current = messages.runtime.read_one("SELECT rowid, batch_id FROM conversation_inputs WHERE event_key = ?",
                                        (event.scoped_key,))
    if current is None:
        raise TaskError("quote_original_unavailable", "group quote requires its admitted original input")
    groups = messages.group_context
    instruction = messages.batch(current["batch_id"])["role_instruction"]
    # Inspect only routing metadata before reading bodies. An already known ID
    # in another group/private scene or persona cannot be republished as new.
    from .models import TelegramQuoteDescriptor
    telegram = isinstance(quote, TelegramQuoteDescriptor)
    # Telegram message IDs are local to a chat; other chats may reuse them.
    rows = messages.runtime.read_all("""SELECT i.*, b.context_json FROM conversation_inputs i
        JOIN conversation_batches b ON b.batch_id = i.batch_id
        WHERE json_extract(i.event_json, '$.event_id') = ?
        AND json_extract(i.event_json, '$.scope.platform') = ?
        AND json_extract(i.event_json, '$.scope.account_id') = ?
        AND (? IS NULL OR json_extract(i.event_json, '$.scope.native_scope_id') = ?) LIMIT 2""",
        (quote.message_id, event.scope.platform, event.scope.account_id,
         event.scope.native_scope_id if telegram else None, event.scope.native_scope_id))
    if len(rows) > 1:
        raise TaskError("quote_original_ambiguous", "known group original is not unique")
    evidence = []
    for row in rows:
        original = TaskContext.restore(json.loads(row["context_json"]))
        if (original.scope != context.scope or original.role_binding != context.role_binding
                or original.actor.principal.native_account_id != quote.sender_id):
            raise TaskError("quote_original_scope_mismatch", "known group original belongs to another speaker or context")
        data = json.loads(row["event_json"])
        if not telegram and data.get("quotes"):
            raise TaskError("quote_original_changed", "nested replies require their own original-source contract")
        item, source = groups.read_input(groups.input_ref(row), context, instruction, at, current["rowid"],
                                         evidence=evidence, include_quoted_media=not telegram)
        from companion_v2.conversation.messages import event_from_document
        saved = event_from_document(data, original)
        body = item["text"]
        prefix = ""
        media_event = saved
        if telegram:
            from .originals import telegram_attachment_caption, telegram_original, telegram_text
            body, media_event, owner = telegram_original(messages, original, saved, quote, body, evidence, at)
            if getattr(quote, "attachment", None):
                body = telegram_attachment_caption(body, owner, quote)
            else:
                prefix, body = telegram_text(body, quote, bool(getattr(media_event, "images", ())))
        if telegram and quote.selection_offset is not None:
            offset = quote.selection_offset
            selected = text.encode("utf-16-le")
            if (type(offset) is not int or offset < 0
                    or body.encode("utf-16-le")[2 * offset:2 * offset + len(selected)] != selected):
                raise TaskError("quote_original_changed", "quoted selection differs from the admitted original")
            evidence.append(source)
            continue
        if telegram and getattr(quote, "attachment", None):
            if body != text:
                raise TaskError("quote_original_changed", "attachment differs from the admitted group original")
            evidence.append(source)
            continue
        if (prefix + body != text or tuple(replace(i, position=0) for i in getattr(media_event, "images", ()))
                != tuple(replace(i, position=0) for i in quote.images)
                or any(getattr(saved, name, ()) != getattr(quote, name, ()) for name in ("audios", "videos"))
                or (not telegram and getattr(saved, "forwards", ()) != getattr(quote, "forwards", ()))):
            raise TaskError("quote_original_changed", "quote differs from the already admitted group original")
        evidence.append(source)
    platform_ref = "onebot-message:" + digest([event.scope.platform, event.scope.account_id])[:24] + ":" + quote.message_id
    replies = messages.runtime.read_all("SELECT * FROM conversation_parts WHERE platform_ref = ? LIMIT 2", (platform_ref,))
    if (rows and replies) or len(replies) > 1:
        raise TaskError("quote_original_ambiguous", "quoted Bot original is not unique")
    for part in replies:
        if quote.sender_id != event.scope.account_id:
            raise TaskError("quote_original_changed", "known Bot reply cannot be attributed to another member")
        images = []
        item, _ = groups.replies.read(groups.reply_ref(part), context, instruction, at, current["rowid"],
                                     evidence=evidence, images=images)
        expected_text = item["text"] + "".join("\n[图片 " + str(n) + "]\n" for n in range(1, len(images) + 1))
        if (expected_text != text or tuple(replace(i.descriptor, position=n) for n, i in enumerate(images, 1)) != quote.images
                or any(getattr(quote, name, ()) for name in ("audios", "videos", "forwards"))):
            raise TaskError("quote_original_changed", "quote differs from the confirmed group reply")
    return tuple(dict.fromkeys(evidence))
