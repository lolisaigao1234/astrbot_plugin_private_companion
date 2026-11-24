"""Combine verified Telegram forwards in the same private debounce window."""

from dataclasses import replace

from astrbot.core.message.components import Plain

from companion_v2.conversation.private_debounce import MAX_MERGED_CHARS

from .core_reply_forwards import MAX_CORE_FORWARD_IMAGES, component_kind


def _bounded_sections(originals, budget=MAX_MERGED_CHARS):
    forward_ids = {item.event_id for item in originals if item.forward_origin}
    replied_to = set()
    headers = []
    for item in originals:
        if item.forward_origin:
            headers.append(f"[转发消息，来源：{item.forward_origin}]\n")
        else:
            reply_id = next((q.message_id for q in getattr(item, "quotes", ()) if q.message_id in forward_ids), None)
            if reply_id:
                replied_to.add(reply_id)
            headers.append(f"[发起人的留言，回复转发消息 {reply_id}]\n" if reply_id else "[发起人的留言]\n")
    bodies = [item.text.removeprefix(header) if item.forward_origin else item.text
              for item, header in zip(originals, headers)]
    # Preserve attribution even for a hundred long messages. Shorten labels only
    # when keeping them in full would leave no space for their message bodies.
    fair_share = budget // len(headers)
    if sum(len(header) + min(32, len(body)) + 2 for header, body in zip(headers, bodies)) > budget:
        label_limit = max(1, fair_share - len("[转发消息，来源：]\n") - 34)
        for i, item in enumerate(originals):
            name = item.forward_origin
            if len(name) > label_limit:
                tail = label_limit // 2
                suffix = name[-tail:] if tail else ""
                headers[i] = f"[转发消息，来源：{name[:label_limit-tail]}…{suffix}]\n"

    for i, item in enumerate(originals):
        if item.event_id in replied_to:
            headers[i] = f"[消息 {item.event_id}]" + headers[i]

    remaining = budget
    sizes = {}
    priority = sorted(reversed(range(len(originals))), key=lambda i: bool(originals[i].forward_origin))
    overflow = sum(len(header) + 3 for header in headers) > remaining
    if overflow:
        remaining -= len(f"[另有 {len(originals)} 条消息未显示]\n")
    for i in priority:
        sample = min(1, len(bodies[i]))
        cost = len(headers[i]) + sample + (2 if sizes else 0)
        if cost <= remaining:
            sizes[i] = sample
            remaining -= cost

    # Give each included body a readable sample before assigning extra space
    # to the sender's comments and longer excerpts.
    if sizes:
        sample_size = min(32, 1 + remaining // len(sizes))
        for i in sizes:
            extra = min(len(bodies[i]), sample_size) - sizes[i]
            sizes[i] += extra
            remaining -= extra

    for forwarded in (False, True):
        indices = sorted((i for i in sizes if bool(originals[i].forward_origin) == forwarded),
                         key=lambda i: len(bodies[i]))
        for count, i in enumerate(indices):
            extra = min(len(bodies[i]) - sizes[i], remaining // (len(indices) - count))
            sizes[i] += extra
            remaining -= extra

    indices = sorted(sizes)
    for position, i in enumerate(indices):
        size = sizes[i]
        body = bodies[i]
        if size == 1 and len(body) > 1:
            body = "…"
        elif size < len(body):
            body = body[:size-1] + "…" if originals[i].forward_origin else "…" + body[-(size-1):]
        prefix = ""
        if position == 0 and len(sizes) < len(originals):
            prefix = f"[另有 {len(originals) - len(sizes)} 条消息未显示]\n"
        yield i, prefix + headers[i] + body


async def merge_forward_burst(event, target, plan, entries, prepare_context):
    verified, parts, sections = [], [], []
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
        verified.append((item, source_event))
    if len(verified) < 2:
        return plan
    # Intake awaits can complete out of order. Telegram orders messages in this chat by ID.
    verified.sort(key=lambda pair: int(pair[0].event_id))
    originals = tuple(item for item, _ in verified)
    media = [[part for part in source_event.message_obj.message
              if component_kind(part) in {"Image", "Record", "Video", "File"}] for _, source_event in verified]
    direct_count = sum(len(parts) for item, parts in zip(originals, media) if not item.forward_origin)
    forward_slots = max(0, remaining_media - direct_count)
    omitted = max(0, sum(map(len, media)) - remaining_media)
    note = f"\n[另有 {omitted} 个附件超过读取上限]" if omitted else ""
    for i, section in _bounded_sections(originals, MAX_MERGED_CHARS - len(note)):
        if sections:
            section = "\n\n" + section
        sections.append(section)
        parts.append(Plain(text=section))
        slots = min(remaining_media, forward_slots) if originals[i].forward_origin else remaining_media
        admitted = media[i][:slots]
        parts.extend(admitted)
        remaining_media -= len(admitted)
        if originals[i].forward_origin:
            forward_slots -= len(admitted)
    if note:
        parts.append(Plain(text=note))
        sections.append(note)
    text = "".join(sections)
    event.message_obj.message = parts
    event.message_str = event.message_obj.message_str = text
    return replace(plan, captured=replace(plan.captured, text=text, forward_origin="",
                   merged_event_ids=tuple(item.event_id for item in originals), telegram_burst=originals))
