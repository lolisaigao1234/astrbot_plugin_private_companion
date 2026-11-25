"""Recipient-scoped platform presence actions."""

import asyncio
from datetime import timedelta

from companion_v2.execution.models import require
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import ScopeKind
from .native import route_id
from .telegram_logging import redact_telegram_token


def native_presence_port(bridge, scope, *, reverse_api_type, authorize, now):
    if bridge.platform_name(scope) == "telegram":
        return TelegramPresencePort(bridge, authorize=authorize, now=now)
    return OneBotPresencePort(bridge, reverse_api_type=reverse_api_type, authorize=authorize, now=now)


class _PresencePort:
    def __init__(self, bridge, *, authorize, now):
        self.bridge, self.authorize, self.now = bridge, authorize, now

    def matches_recipient(self, context):
        scope, principal = context.scope, context.actor.principal
        return (context.audience.recipients == (context.actor.subject,)
                and principal.host_id == self.bridge.host_id and principal.account_id == scope.account_id
                and principal.bot_id == scope.bot_id and principal.platform == scope.platform
                and route_id(principal.native_account_id) == scope.native_scope_id)

    def check_access(self, context, capability):
        check = self.authorize()
        if isinstance(check, Outcome):
            require(check)
        require(self.bridge.identities.authorize(context.actor, scope=context.scope,
                capability=capability, observed_at=self.now()))


class TelegramPresencePort(_PresencePort):
    async def typing(self, context, *, before_dispatch=None):
        scope = context.scope
        try:
            if scope.kind is not ScopeKind.PRIVATE or not self.matches_recipient(context):
                return {"state": "not_started", "code": "presence_scope_mismatch"}
            chat_id = int(scope.native_scope_id)
            if chat_id <= 0 or str(chat_id) != scope.native_scope_id:
                return {"state": "not_started", "code": "presence_scope_mismatch"}
            self.check_access(context, "platform.typing")
            lease = self.bridge.connections.acquire(self.bridge.account(scope))
            if lease.platform.meta().name != "telegram":
                return {"state": "not_started", "code": "presence_transport_unavailable"}
            await self.bridge.verify_role(scope, context.role_binding)
            self.check_access(context, "platform.typing")
            if before_dispatch is not None:
                before_dispatch()
            self.bridge.connections.assert_current(lease)
        except Exception:
            return {"state": "not_started", "code": "presence_authorization_changed"}
        from telegram.error import BadRequest, NetworkError, RetryAfter
        try:
            with redact_telegram_token(lease.client):
                accepted = await asyncio.wait_for(
                    lease.client.send_chat_action(chat_id=chat_id, action="typing"), timeout=2)
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else delay
            return {"state": "failed", "code": "telegram_rate_limited", "retry_after": max(1, seconds)}
        except BadRequest:
            return {"state": "failed", "code": "presence_action_rejected"}
        except (NetworkError, asyncio.TimeoutError):
            return {"state": "unknown", "code": "presence_ack_unknown", "retry_after": 4}
        except Exception:
            return {"state": "unknown", "code": "presence_ack_unknown"}
        if accepted is True:
            # Telegram clears chat actions after five seconds.
            return {"state": "accepted", "max_refresh_seconds": 4}
        return {"state": "failed", "code": "presence_action_rejected"}

    async def custom_status(self, context, wording, *, before_dispatch=None):
        return {"state": "not_started", "code": "presence_unsupported"}

    async def online_status(self, context, mode, *, before_dispatch=None):
        return {"state": "not_started", "code": "presence_unsupported"}


class OneBotPresencePort(_PresencePort):
    def __init__(self, bridge, *, reverse_api_type, authorize, now):
        super().__init__(bridge, authorize=authorize, now=now)
        self.reverse_api_type = reverse_api_type

    async def _call(self, context, capability, action, *, before_dispatch=None, **parameters):
        try:
            scope = context.scope
            if not self.matches_recipient(context):
                return {"state": "not_started", "code": "presence_scope_mismatch"}
            self.check_access(context, capability)
            await self.bridge.verify_role(scope, context.role_binding)
            client = self.bridge.client(scope)
            api = client._api._wsr_api
            if (type(api) is not self.reverse_api_type or api._api_clients is not client._wsr_api_clients
                    or not client._wsr_api_clients.get(scope.account_id)):
                return {"state": "not_started", "code": "presence_transport_unavailable"}
            self.check_access(context, capability)
            if before_dispatch is not None:
                before_dispatch()
        except Exception:
            return {"state": "not_started", "code": "presence_authorization_changed"}
        try:
            value = await api.call_action(action, self_id=scope.account_id, **parameters)
            if value is None or isinstance(value, dict) and value.get("retcode", 0) == 0 and value.get("status", "ok") in ("ok", "async"):
                return {"state": "accepted"}
            # NapCat's OneBot implementation may acknowledge status actions with
            # a plain success string instead of an empty/dict payload.
            status_ack = {"set_diy_online_status": "set status success",
                          "set_online_status": "set online status success",
                          "set_input_status": "set input status success"}
            if isinstance(value, str) and value.strip().lower() == status_ack.get(action):
                return {"state": "accepted"}
            return {"state": "failed", "code": "presence_action_rejected"}
        except Exception:
            return {"state": "unknown", "code": "presence_ack_unknown"}

    async def typing(self, context, *, before_dispatch=None):
        return await self._call(context, "platform.typing", "set_input_status",
                                user_id=int(context.scope.native_scope_id), event_type=1, before_dispatch=before_dispatch)

    async def custom_status(self, context, wording, *, before_dispatch=None):
        if type(wording) is not str or not wording.strip() or len(wording) > 8:
            return {"state": "not_started", "code": "presence_wording_invalid"}
        return await self._call(context, "platform.presence", "set_diy_online_status",
                                face_id=21, face_type=1, wording=wording, before_dispatch=before_dispatch)

    async def online_status(self, context, mode, *, before_dispatch=None):
        status = {"online": 10, "away": 30, "busy": 50, "invisible": 40}.get(mode) if type(mode) is str else None
        if status is None:
            return {"state": "not_started", "code": "presence_mode_invalid"}
        return await self._call(context, "platform.presence", "set_online_status",
                                status=status, ext_status=0, battery_status=0, before_dispatch=before_dispatch)
