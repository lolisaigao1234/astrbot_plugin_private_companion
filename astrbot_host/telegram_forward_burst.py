"""Combine verified Telegram forwards in the same private debounce window."""

from dataclasses import replace

from astrbot.core.message.components import Plain

from companion_v2.conversation.private_debounce import MAX_MERGED_CHARS

from .core_reply_forwards import MAX_CORE_FORWARD_IMAGES, component_kind


async def merge_forward_burst(event, target, plan, entries, prepare_context):
    originals, parts, sections = [], [], []
    remaining_text = MAX_MERGED_CHARS
    remaining_media = MAX_CORE_FORWARD_IMAGES
    for source_event, _, _ in entries:
        source = plan
        if source_event is not event:
            result = await prepare_context(source_event, target, group_admission=None)
            if result.failure is not None:
                continue
            source = result.unwrap()
        item = source.captured
        if (source.connection_lease != plan.connection_lease or source.context.key != plan.context.key
                or item.principal != plan.captured.principal or item.route != plan.captured.route
                or item.role != plan.captured.role or item.recipients != plan.captured.recipients):
            continue
        originals.append(item)
        header = f"[转发消息，来源：{item.forward_origin}]\n" if item.forward_origin else "[发起人的留言]\n"
        body = item.text.removeprefix(header) if item.forward_origin else item.text
        prefix = ("\n\n" if sections else "") + header
        # Only include media alongside a complete source label.
        if remaining_text <= len(prefix):
            continue
        section = prefix + body[:remaining_text - len(prefix)]
        remaining_text -= len(section)
        sections.append(section)
        parts.append(Plain(text=section))
        for part in source_event.message_obj.message:
            if remaining_media and component_kind(part) in {"Image", "Record", "Video", "File"}:
                parts.append(part)
                remaining_media -= 1
    if len(originals) < 2 or not any(item.forward_origin for item in originals):
        return plan
    text = "".join(sections)
    event.message_obj.message = parts
    event.message_str = event.message_obj.message_str = text
    return replace(plan, captured=replace(plan.captured, text=text, forward_origin="",
                   merged_event_ids=tuple(item.event_id for item in originals), telegram_burst=tuple(originals)))
