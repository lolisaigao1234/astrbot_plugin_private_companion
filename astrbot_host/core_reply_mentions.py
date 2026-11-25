"""Restore group mention positions in the host-owned, persisted user message."""

from collections.abc import Mapping
import logging
import re

from companion_v2.adapters.astrbot.telegram_reply import telegram_has_reply_prefix, telegram_plain_text
from companion_v2.conversation.group_timing import SELF_MENTION
from companion_v2.sources.privacy import redact_text


logger = logging.getLogger(__name__)
BARE_MENTION_SENTINEL_PROMPT = " "
# What attachment preparation appended to the member's words for the model.
TELEGRAM_ATTACHMENT_NOTES = "_private_companion_telegram_attachment_notes"


async def _resolve_session_conversation(event, native_context):
    """Load or create the session conversation so explicit ProviderRequest keeps history."""
    conv_mgr = getattr(native_context, "conversation_manager", None)
    umo = getattr(event, "unified_msg_origin", None)
    if conv_mgr is None or not isinstance(umo, str) or not umo:
        return None
    get_curr = getattr(conv_mgr, "get_curr_conversation_id", None)
    get_conv = getattr(conv_mgr, "get_conversation", None)
    new_conv = getattr(conv_mgr, "new_conversation", None)
    if not (callable(get_curr) and callable(get_conv)):
        return None
    try:
        cid = await get_curr(umo)
        if not cid and callable(new_conv):
            platform_getter = getattr(event, "get_platform_id", None)
            platform_id = platform_getter() if callable(platform_getter) else None
            cid = await new_conv(umo, platform_id)
        conversation = await get_conv(umo, cid) if cid else None
        if conversation is None and callable(new_conv):
            platform_getter = getattr(event, "get_platform_id", None)
            platform_id = platform_getter() if callable(platform_getter) else None
            cid = await new_conv(umo, platform_id)
            conversation = await get_conv(umo, cid) if cid else None
        return conversation
    except Exception as error:
        logger.warning("[CompanionV2] 裸 @ 会话读取降级 error_type=%s", type(error).__name__)
        return None


async def prepare_bare_mention_request(event, *, native_context=None):
    """Keep bare self-mentions deliverable without polluting ``event.message_str``.

    AstrBot's ``InternalAgentSubStage`` drops events that have neither text,
    media/reply components, nor an explicit ``provider_request``; writing
    ``<提及自己>`` into ``event.message_str`` would pass that placeholder into
    AstrBot's automatic knowledge-base retrieval query.  Attaching a
    whitespace-prompt ``ProviderRequest`` bound to the session conversation
    admits the turn while letting ``_apply_kb()`` skip retrieval cleanly, and
    :func:`restore_bare_mention_prompt` restores ``<提及自己>`` afterwards in
    ``on_llm_request``.
    """
    setter = getattr(event, "set_extra", None)
    getter = getattr(event, "get_extra", None)
    if callable(setter):
        setter("enable_streaming", False)
    if not callable(setter):
        return False
    if callable(getter) and getter("provider_request") is not None:
        return True
    try:
        from astrbot.core.provider.entities import ProviderRequest
    except Exception:
        return False
    conversation = await _resolve_session_conversation(event, native_context)
    if conversation is None:
        return False
    umo = getattr(event, "unified_msg_origin", "") or ""
    selected_model = getter("selected_model") if callable(getter) else None
    req = ProviderRequest(
        prompt=BARE_MENTION_SENTINEL_PROMPT,
        session_id=umo,
        image_urls=[],
        audio_urls=[],
        conversation=conversation,
        model=selected_model or None,
    )
    if hasattr(req, "retrieval_query"):
        req.retrieval_query = ""
    setter("provider_request", req)
    return True


def restore_bare_mention_prompt(request, prepared):
    """Restore ``<提及自己>`` after AstrBot's pre-hook KB retrieval has run."""
    if not isinstance(prepared, Mapping):
        return
    if not (prepared.get("group_trigger") or {}).get("bare_mention"):
        return
    prompt = getattr(request, "prompt", None)
    if not isinstance(prompt, str) or not prompt.strip():
        request.prompt = SELF_MENTION
    if hasattr(request, "retrieval_query"):
        current_query = getattr(request, "retrieval_query", None)
        if current_query is None or str(current_query).strip() == SELF_MENTION:
            request.retrieval_query = ""


def telegram_reference_prefix(event, original):
    origin = getattr(original, "forward_origin", "")
    markers = [f"[转发消息，来源：{origin}]"] if origin else []
    if getattr(original, "quote_unavailable", False):
        markers.append("[引用消息不可用]")
    for quote in getattr(original, "quotes", ()):
        raw = getattr(getattr(getattr(event, "message_obj", None), "raw_message", None), "message", None)
        # Capture keeps the original label so a later check can prove it unchanged.
        if getattr(raw, "voice", None) and not any(
                getattr(part.type, "value", part.type) == "Reply" for part in event.message_obj.message):
            markers.append(redact_text(quote.observation_text()))
        elif getattr(quote, "forward_origin", None):
            attribution = redact_text(quote.forward_origin.attribution(quote.sender_id))
            markers.append(f"[引用消息 {quote.message_id}，{attribution}]")
    return "\n".join(markers) + "\n" if markers else ""


def restore_group_mentions(event, request, native_input, native_context):
    original = getattr(native_input, "group_message_text", None)
    plain = getattr(native_input, "text", None)
    prompt = getattr(request, "prompt", None)
    if not isinstance(original, str) or not isinstance(plain, str) or not isinstance(prompt, str):
        return
    prefix = telegram_reference_prefix(event, native_input)
    applied_prefix = prefix if prefix and prompt.startswith(prefix) else ""
    if applied_prefix:
        prompt = prompt[len(applied_prefix):]
    message = getattr(event, "message_obj", None)
    if prefix or getattr(getattr(getattr(message, "raw_message", None), "message", None), "voice", None):
        # AstrBot's own text has neither these markers nor a voice note's caption.
        plain = telegram_plain_text(event, message.message_str)
    getter = getattr(event, "get_extra", None)
    notes = getter(TELEGRAM_ATTACHMENT_NOTES) if callable(getter) else None
    applied_suffix = ""
    if notes and prompt.endswith(notes):
        applied_suffix = "\n" + notes
        prompt = prompt.removesuffix(notes).removesuffix("\n")
        plain = plain.removesuffix(notes).removesuffix("\n")
    # A custom wake prefix leaves the marker AstrBot adds for a reply in the prompt.
    marked = event.message_obj.message_str if telegram_has_reply_prefix(event) else plain
    if original == plain == marked:
        return
    config = {}
    getter = getattr(native_context, "get_config", None)
    if callable(getter):
        config = getter(umo=getattr(event, "unified_msg_origin", None))
    provider = config.get("provider_settings", {}) if isinstance(config, Mapping) else {}
    prefixes = list(config.get("wake_prefix", ())) if getattr(event, "is_at_or_wake_command", False) else []
    prefixes.append(provider.get("wake_prefix", ""))
    variants = [(plain, original), (plain.strip(), original.strip()),
                (marked, original), (marked.strip(), original.strip())]
    if not plain.strip():
        variants.append((BARE_MENTION_SENTINEL_PROMPT, original))
    if not plain.strip() and any(
            getattr(getattr(part, "type", None), "value", getattr(part, "type", None))
            in {"Reply", "Image", "Record", "Video", "File", "Forward"}
            for part in getattr(getattr(event, "message_obj", None), "message", ())):
        variants.append(("<attachment>", original))
    # Strip only configured command/provider prefixes. Keep a leading native
    # self mention when the command prefix followed that mention in the source.
    for prefix in prefixes:
        if not isinstance(prefix, str) or not prefix:
            continue
        for before, after in tuple(variants):
            if not before.startswith(prefix):
                continue
            leading = re.match(r"(?:" + re.escape(SELF_MENTION) + r"\s*)*", after).end()
            if after[leading:].startswith(prefix):
                remainder = after[:leading] + after[leading + len(prefix):]
                variants.extend(((before[len(prefix):], remainder),
                                 (before[len(prefix):].strip(), remainder.strip())))
    template = provider.get("prompt_prefix", "")
    for before, after in tuple(variants):
        if template:
            variants.append((template.replace("{{prompt}}", before), template.replace("{{prompt}}", after))
                if "{{prompt}}" in template else (template + before, template + after))
    for before, after in variants:
        if prompt == before:
            request.prompt = applied_prefix + after + applied_suffix
            return
