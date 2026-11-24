"""Components the Telegram SDK can render through both session and event sends."""

import asyncio
from functools import partial

from astrbot.core.message.components import ActionRow, At, Face, File, Image, Plain, Record, Reply, Video
from astrbot.core.utils.media_utils import convert_audio_format

from .telegram_logging import redact_telegram_token


class _TelegramVoice(Record):
    async def convert_to_file_path(self):
        # AstrBot's Record resolves every source to WAV, including encoded input.
        wav = await super().convert_to_file_path()
        return await convert_audio_format(wav, output_format="ogg")


def telegram_component(part):
    if isinstance(part, Face):
        return Plain(text=f"[QQ表情 {part.id}]")
    if isinstance(part, Record):
        return _TelegramVoice(file=part.file, url=part.url, path=part.path, text=part.text)
    return part


def telegram_visible(chain):
    return any(isinstance(part, (Image, File, Video, Record, Face, ActionRow))
               or isinstance(part, Plain) and part.text.strip() for part in chain)


def telegram_mentions(chain, usernames, *, kind=At):
    """Fold planned mentions into the first text AstrBot sends to Telegram."""
    mentions = []
    result = []
    for part in chain:
        if not isinstance(part, kind):
            result.append(part)
            continue
        identifier = str(part.qq)
        if identifier.isascii() and identifier.isdigit() and int(identifier) > 0:
            username = usernames.get(identifier)
            mentions.append(f"@{username}" if username else f"[成员](tg://user?id={identifier})")
    if mentions:
        prefix = " ".join(mentions)
        for index, part in enumerate(result):
            if isinstance(part, Plain):
                result[index] = Plain(text=f"{prefix} {part.text}")
                break
        else:
            result.insert(0, Plain(text=prefix))
    return result


class _OptionalReplyClient:
    """Add the Bot API option at each individual send, including media and chunks."""

    def __init__(self, bot):
        self.bot = bot

    def __getattr__(self, name):
        method = getattr(self.bot, name)
        if name in {"send_message", "send_photo", "send_animation", "send_document", "send_voice", "send_video"}:
            return partial(method, allow_sending_without_reply=True)
        return method


async def send_optional_reply(client, chain, window):
    from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
    await TelegramPlatformEvent.send_with_client(_OptionalReplyClient(client), chain, window)


class _OptionalReply(Reply):
    pass


class _MemberMention(At):
    pass


def decorate_event_headers(event, chain, mentions, quote):
    """Keep native headers visible to tracking until the SDK serializes them."""
    author = event.message_obj.raw_message.message.from_user
    if not getattr(event.send_with_client, "_companion_group_headers", False):
        original = event.send_with_client

        async def send(client, message, window):
            members = {str(part.qq): part.name for part in message.chain if isinstance(part, _MemberMention)}
            if members:
                # Only translate headers selected by this plugin.
                from astrbot.core.message.message_event_result import MessageChain
                message = MessageChain(chain=telegram_mentions(message.chain, members, kind=_MemberMention))
            if any(isinstance(part, _OptionalReply) for part in message.chain):
                client = _OptionalReplyClient(client)
            await original(client, message, window)

        send._companion_group_headers = True
        event.send_with_client = send

    return ([_OptionalReply(id=quote)] if quote else []) + [
        _MemberMention(qq=part.qq, name=author.username if str(part.qq) == str(author.id) else None)
        if any(part is header for header in mentions) else part for part in chain
    ]


async def member_usernames(client, window, identifiers):
    """A delayed plan retains IDs; resolve current usernames in the same chat."""
    lookup = getattr(client, "get_chat_member", None)
    if not identifiers or not callable(lookup):
        return {}
    chat_id = window.partition("#")[0]

    async def username(identifier):
        try:
            with redact_telegram_token(client):
                member = await asyncio.wait_for(lookup(chat_id=chat_id, user_id=int(identifier)), 2)
            if str(member.user.id) == identifier:
                return identifier, member.user.username
        except Exception:
            # A bot may lack member lookup permission. The ID link still works.
            pass
        return identifier, None

    return dict(await asyncio.gather(*(username(identifier) for identifier in identifiers)))
