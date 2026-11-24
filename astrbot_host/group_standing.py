"""Read group standing from authenticated OneBot events and Telegram memberships."""

from collections.abc import Mapping
from time import monotonic

ROLES = ("owner", "admin", "member")


def group_sender_standing(event):
    """Return ``{"account_id", "role", "group_id"}`` for the sender, or ``None``."""
    message = getattr(event, "message_obj", None)
    raw = getattr(message, "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    sender = raw.get("sender")
    if not isinstance(sender, Mapping):
        return None
    role = sender.get("role")
    if not isinstance(role, str):
        return None
    role = role.strip().lower()
    if role not in ROLES:
        return None
    account = sender.get("user_id")
    if type(account) is int:
        account = str(account)
    if not isinstance(account, str):
        return None
    account = account.strip()
    if not (account.isascii() and account.isdigit()):
        return None
    group = raw.get("group_id")
    if type(group) is int:
        group = str(group)
    return {"account_id": account, "role": role,
            "group_id": group.strip() if isinstance(group, str) else ""}


async def resolve_group_standing(event, cache, *, fresh=False):
    """Read standing for a validated group event. Topics share chat membership."""
    if getattr(getattr(event, "platform_meta", None), "name", "") != "telegram":
        return group_sender_standing(event)
    from companion_v2.adapters.astrbot.group import group_event_route
    from companion_v2.adapters.astrbot.native import bounded_native_lookup
    from companion_v2.adapters.astrbot.telegram_logging import redact_telegram_token

    route = group_event_route(event)
    chat = event.message_obj.raw_message.message.chat.id
    client = event.client
    key = (route.platform_id, chat, route.sender_id)
    if fresh:
        cache.pop(key, None)
    entry = cache.get(key)
    if entry and entry[0] is client and entry[1] > monotonic():
        role = entry[2]
    else:
        try:
            with redact_telegram_token(client):
                member = await bounded_native_lookup(
                    client.get_chat_member(chat_id=chat, user_id=int(route.sender_id)), operation="group_standing")
            if str(member.user.id) != route.sender_id:
                return None
            role = {"creator": "owner", "administrator": "admin", "member": "member"}.get(member.status)
        except Exception:
            return None
        if role is None:
            return None
        if len(cache) >= 1024:
            cache.pop(next(iter(cache)))
        cache[key] = client, monotonic() + 30, role
    return {"account_id": route.sender_id, "role": role, "group_id": route.group_id}
