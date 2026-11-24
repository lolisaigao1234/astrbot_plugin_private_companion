"""Combine verified Telegram forwards in the same private debounce window."""

from dataclasses import replace

from astrbot.core.message.components import Plain

from companion_v2.conversation.private_debounce import MAX_MERGED_CHARS

from .binding import BURST_SENDER_TEXT_EXTRA
from .core_reply_forwards import MAX_CORE_FORWARD_IMAGES, component_kind, quoted_telegram_forward


def _quoted_lines(text):
    # Keep user-written section markers visibly inside the body that supplied them.
    return "\n".join("│ " + line if line.lstrip().startswith("[") else line
                     for line in text.splitlines())


def sender_section(original, prompt, text):
    """Keep a comment folded into a forward's pending turn apart from the forwarded words."""
    if getattr(original, "forward_origin", "") and isinstance(prompt, str):
        prompt = _quoted_lines(prompt)
    return prompt, "[发起人的留言]\n" + _quoted_lines(text)


def _origin_label(name):
    return " ".join(name.splitlines()).replace("[", "［").replace("]", "］")


def _bounded_sections(originals, budget=MAX_MERGED_CHARS, replies=None):
    replies = replies or {}
    forward_ids = {item.event_id for item in originals if item.forward_origin}
    replied_to = set()
    headers, bodies = [], []
    for item in originals:
        body = item.text
        if item.forward_origin:
            body = body.removeprefix(f"[转发消息，来源：{item.forward_origin}]\n")
            header = f"[转发消息，来源：{_origin_label(item.forward_origin)}]\n"
        else:
            quote = next(iter(getattr(item, "quotes", ())), None)
            target = replies.get(item.event_id, quote.message_id) if quote else None
            header = "[发起人的留言]\n"
            if target in forward_ids:
                replied_to.add(target)
                header = f"[发起人的留言，回复转发消息 {target}]\n"
                if getattr(quote, "selection_offset", None) is not None:
                    allowance = max(12, budget // len(originals) - len(header) - 36)
                    selected = quote.text if len(quote.text) <= allowance else quote.text[:allowance-1] + "…"
                    header = f"[回复转发消息 {target}]\n所选片段：{_quoted_lines(selected)}\n[发起人的留言]\n"
        headers.append(header)
        bodies.append(_quoted_lines(body))
    # Preserve attribution even for a hundred long messages. Shorten labels only
    # when keeping them in full would leave no space for their message bodies.
    fair_share = budget // len(headers)
    if sum(len(header) + min(32, len(body)) + 2 for header, body in zip(headers, bodies)) > budget:
        label_limit = max(1, fair_share - len("[转发消息，来源：]\n") - 34)
        for i, item in enumerate(originals):
            name = _origin_label(item.forward_origin)
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


async def bounded_file_component(event, locator, remaining, preferences, scope, deadline):
    import aiohttp
    import asyncio
    from pathlib import Path
    from tempfile import NamedTemporaryFile
    from astrbot.core.message.components import File, Plain
    from companion_v2.conversation.media.understanding import media_scope_enabled
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    from companion_v2.adapters.astrbot.telegram_media import fetch_location
    from companion_v2.execution.models import TaskError
    from .media import fetch_attachment

    # SDK File.get_file would download the original without the burst's remaining cap.
    if not preferences["file_understanding_enabled"] or not media_scope_enabled(preferences, "file_understanding", scope):
        return Plain(f"[文件 {locator.name}：文件理解未启用，未读取正文]"), 0
    if Path(locator.name).suffix.lower() not in preferences["file_understanding_allowed_suffixes"]:
        return Plain(f"[文件 {locator.name}：文件格式暂不支持]"), 0
    time_left = deadline - asyncio.get_running_loop().time()
    if time_left <= 0:
        return Plain(f"[文件 {locator.name}：文件读取超时，本轮未读取]"), 0
    maximum_bytes = min(remaining, preferences["file_understanding_max_size_mb"] * 1024**2)
    try:
        data = await asyncio.wait_for(fetch_attachment(fetch_location(locator), maximum_bytes=maximum_bytes),
                                      time_left)
    except TimeoutError:
        # A cancelled prefetch can have consumed at most the offered document allowance.
        return Plain(f"[文件 {locator.name}：文件读取超时，本轮未读取]"), maximum_bytes
    except (aiohttp.ClientError, OSError, RuntimeError):
        raise TaskError("host_file_unavailable", "document could not be retrieved") from None
    directory = Path(get_astrbot_temp_path())
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=directory, suffix=Path(locator.name).suffix[:20], delete=False) as stream:
        event.track_temporary_local_file(stream.name)
        stream.write(data)
    return File(name=locator.name, file=stream.name), len(data)


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
    if not verified or len(entries) < 2:
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
    if len(verified) < len(entries):
        note += f"\n[另有 {len(entries) - len(verified)} 条消息未通过核验，本轮未读取]"
    # A reply may quote any photo of an album that AstrBot merged under its first message.
    replies = {item.event_id: forward.event_id for item, source_event in verified if not item.forward_origin
               if (matches := quoted_telegram_forward(source_event))
               for forward, candidate in verified if matches(candidate)}
    for i, section in _bounded_sections(originals, MAX_MERGED_CHARS - len(note), replies):
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
    event.set_extra(BURST_SENDER_TEXT_EXTRA, "\n".join(
        str(source_event.message_str or "") for item, source_event in verified if not item.forward_origin))
    event.message_obj.message = parts
    event.message_str = event.message_obj.message_str = text
    # The sections above already show which collected forward a comment replies to.
    quotes = () if plan.captured.event_id in replies else plan.captured.quotes
    return replace(plan, captured=replace(plan.captured, text=text, forward_origin="", quotes=quotes,
                   merged_event_ids=tuple(item.event_id for item in originals), telegram_burst=originals))
