"""Dispatch durable relay tasks from authenticated inbound events."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from companion_v2.foundation.refs import ScopeKind

from .core_reply_forwards import telegram_private_forward
from .private_notice import prepare_relay_transport, send_group_notice, send_private_notice
from .relay_pending import (claim_expired, claim_group, claim_private_receipts, claim_telegram_receipt, finish,
                            mark_waiting_permission, requeue)


def _text(value, limit=800):
    return " ".join(str(value or "").split())[:limit]


def _telegram_account(event, target):
    if getattr(getattr(event, "platform_meta", None), "name", "") == "telegram":
        return [target.platform_id, target.account_id]
    return None


def _platform_for(controller, platform_id):
    bridge = getattr(getattr(controller, "session", None), "bridge", None)
    pinned = getattr(bridge, "platforms", None)
    if isinstance(pinned, dict):
        item = pinned.get(platform_id)
        if isinstance(item, tuple) and item:
            return item[0]
    return None


def _route(event, controller, *, kind, platform_id, login, peer):
    if getattr(getattr(event, "platform_meta", None), "name", "") == "telegram":
        from dataclasses import replace
        from .recalls import authenticate_notice_route
        from companion_v2.execution.models import TaskError
        origin = ScopeKind.GROUP if event.message_obj.group_id else ScopeKind.PRIVATE
        original = authenticate_notice_route(controller, event, origin, expected_post="message")
        if original is None or (original.platform_id, original.login) != (platform_id, login):
            raise TaskError("relay_account_unavailable", "无法验证递话来源")
        return replace(original, kind=kind, peer=str(peer), session_id=str(peer))
    return SimpleNamespace(
        kind=kind,
        platform_id=platform_id,
        login=login,
        peer=str(peer),
        client=getattr(event, "bot", None),
        platform=_platform_for(controller, platform_id),
        platform_name=str(getattr(getattr(event, "platform_meta", None), "name", "") or ""),
    )


async def _send_private(controller, event, *, platform_id, login, peer, text, require_message_id=False):
    route = _route(event, controller, kind=ScopeKind.PRIVATE, platform_id=platform_id, login=login, peer=peer)
    transport = prepare_relay_transport(controller, route)
    return await send_private_notice(controller, None, text=text,
                                     route=route, transport=transport, require_message_id=require_message_id)


async def _send_group(controller, event, *, platform_id, login, peer, text, mention_id=""):
    route = _route(event, controller, kind=ScopeKind.GROUP, platform_id=platform_id, login=login, peer=peer)
    transport = prepare_relay_transport(controller, route)
    return await send_group_notice(controller, route, text=text,
                                   mention_id=mention_id, transport=transport)


async def process_group_presence(controller, event, target, context):
    """Deliver delayed relays when their target member speaks in the group."""
    target_user_id = str(context.actor.principal.native_account_id)
    tasks = await asyncio.to_thread(claim_group, controller.runtime, str(target.group_id), target_user_id,
                                    telegram_account=_telegram_account(event, target))
    settled: set[str] = set()
    in_flight: str | None = None
    in_flight_state = "failed"
    try:
        for task in tasks:
            task_id = task["id"]
            in_flight = task_id
            in_flight_state = "unknown" if task.get("telegram_account") else "failed"
            try:
                result = await _send_group(controller, event, platform_id=target.platform_id,
                    login=target.account_id, peer=target.group_id,
                    text=_text(task.get("text")), mention_id=str(task.get("mention_id") or ""))
                state = result.get("state") if isinstance(result, dict) else None
                in_flight_state = {"delivered": "sent", "unknown": "unknown"}.get(state, "failed")
            except Exception:
                in_flight_state = "failed"
            await asyncio.to_thread(finish, controller.runtime, task_id, in_flight_state)
            settled.add(task_id)
            in_flight = None
    finally:
        for task in tasks:
            task_id = task["id"]
            if task_id in settled:
                continue
            try:
                if task_id == in_flight:
                    finish(controller.runtime, task_id, in_flight_state)
                else:
                    requeue(controller.runtime, task_id, state="waiting")
            except Exception:
                pass
    # 带话一直没等到目标成员出现时，告诉发起窗口“已经撤回”，
    # 而不是让那条委托在队列里无声过期。
    expired = await asyncio.to_thread(claim_expired, controller.runtime, str(target.group_id),
                                      telegram_account=_telegram_account(event, target))
    for task in expired:
        source_kind = str(task.get("source_kind") or "private")
        source_id = str(task.get("source_id") or task.get("source_user") or "")
        if not source_id:
            continue
        text = "你让我在群里带的那句话，对方一直没出现，我先撤回了。"
        try:
            if source_kind == "group":
                await _send_group(controller, event, platform_id=target.platform_id,
                    login=target.account_id, peer=source_id, text=text)
            else:
                await _send_private(controller, event, platform_id=target.platform_id,
                    login=target.account_id, peer=source_id, text=text)
        except Exception:
            continue


async def _report_receipt(controller, event, target, task, reply_text):
    source_kind = str(task.get("source_kind") or "private")
    source_id = str(task.get("source_id") or task.get("source_user") or "")
    if not source_id:
        return "failed"
    report = "你让我问的「%s」有回复了：%s" % (
        _text(task.get("question"), 120), _text(reply_text))
    if source_kind == "group":
        result = await _send_group(controller, event, platform_id=target.platform_id,
            login=target.account_id, peer=source_id, text=report)
    else:
        result = await _send_private(controller, event, platform_id=target.platform_id,
            login=target.account_id, peer=source_id, text=report)
    return {"delivered": "reported", "unknown": "unknown"}.get(result.get("state"), "failed")


async def _ask_permission(controller, event, target, task):
    telegram = _telegram_account(event, target) is not None
    prompt = ("可以把这条回复带回给发起人吗？请回复这条消息，说「可以」或「不要」。"
              if telegram else "可以把这条回复带回给发起人吗？回复「可以」或「不要」。")
    return await _send_private(controller, event, platform_id=target.platform_id,
        login=target.account_id, peer=target.participant_id, text=prompt, require_message_id=telegram)


async def process_private_receipts(controller, event, target):
    """Handle a target member's reply and optionally ask before reporting it."""
    if telegram_private_forward(event):
        return
    target_user_id = str(target.participant_id)
    text = _text(getattr(event, "message_str", ""), 800)
    approvals = ("可以", "同意", "好", "好的", "行", "转告", "带回去", "转发")
    refusals = ("不要", "不用", "不行", "不可以", "拒绝", "别转", "算了")
    account = _telegram_account(event, target)
    if account is None:
        confirmed = any(word in text for word in approvals)
        rejected = "不同意" in text or "不好" in text.replace("不好意思", "") or any(word in text for word in refusals)
        permission_tasks = await asyncio.to_thread(
            claim_private_receipts, controller.runtime, target_user_id, permission=True)
    else:
        raw = getattr(getattr(event.message_obj, "raw_message", None), "message", None)
        answer = _text(getattr(raw, "text", ""), 800).rstrip("。！!.").strip()
        confirmed, rejected = answer in approvals, answer in (*refusals, "不同意", "不好")
        original = getattr(raw, "reply_to_message", None)
        reply_to = None
        if original is not None:
            reply_to = (original.message_id if original.chat.id == raw.chat.id else -1)
        permission_tasks, reply_tasks = await asyncio.to_thread(
            claim_telegram_receipt, controller.runtime, account, target_user_id,
            getattr(raw, "message_id", None), reply_to=reply_to, permission_answer=confirmed or rejected)
    settled_permission: set[str] = set()
    in_flight_permission: str | None = None
    in_flight_permission_state = "failed"
    try:
        for task in permission_tasks:
            task_id = task["id"]
            if rejected:
                in_flight_permission = task_id
                in_flight_permission_state = "declined"
                await asyncio.to_thread(finish, controller.runtime, task_id, "declined")
            elif confirmed:
                in_flight_permission = task_id
                in_flight_permission_state = "unknown"
                try:
                    in_flight_permission_state = await _report_receipt(
                        controller, event, target, task, _text(task.get("reply_text"), 800))
                except Exception:
                    in_flight_permission_state = "failed"
                await asyncio.to_thread(finish, controller.runtime, task_id, in_flight_permission_state)
            else:
                await asyncio.to_thread(requeue, controller.runtime, task_id, state="waiting_permission")
            settled_permission.add(task_id)
            in_flight_permission = None
    finally:
        for task in permission_tasks:
            task_id = task["id"]
            if task_id in settled_permission:
                continue
            try:
                if task_id == in_flight_permission:
                    finish(controller.runtime, task_id, in_flight_permission_state)
                else:
                    requeue(controller.runtime, task_id, state="waiting_permission")
            except Exception:
                pass

    if account is None:
        reply_tasks = await asyncio.to_thread(claim_private_receipts, controller.runtime, target_user_id)
    settled_reply: set[str] = set()
    in_flight_report: str | None = None
    in_flight_report_state = "failed"
    in_flight_ask: str | None = None
    prompt_id = None
    try:
        for task in reply_tasks:
            task_id = task["id"]
            if task.get("confirm_before_report"):
                in_flight_report = task_id if account is not None else None
                in_flight_report_state = "unknown"
                try:
                    asked = await _ask_permission(controller, event, target, task)
                except Exception:
                    asked = {"state": "failed"}
                if asked.get("state") == "unknown":
                    await asyncio.to_thread(finish, controller.runtime, task_id, "unknown")
                elif asked.get("state") == "delivered":
                    in_flight_report = None
                    in_flight_ask = task_id
                    prompt_id = asked.get("message_id")
                    await asyncio.to_thread(mark_waiting_permission, controller.runtime, task_id, text, prompt_id)
                else:
                    in_flight_report = None
                    await asyncio.to_thread(requeue, controller.runtime, task_id, state="waiting_reply")
            else:
                in_flight_report = task_id
                in_flight_report_state = "unknown"
                try:
                    in_flight_report_state = await _report_receipt(controller, event, target, task, text)
                except Exception:
                    in_flight_report_state = "failed"
                await asyncio.to_thread(finish, controller.runtime, task_id, in_flight_report_state)
            settled_reply.add(task_id)
            in_flight_report = None
            in_flight_ask = None
    finally:
        for task in reply_tasks:
            task_id = task["id"]
            if task_id in settled_reply:
                continue
            try:
                if task_id == in_flight_report:
                    finish(controller.runtime, task_id, in_flight_report_state)
                elif task_id == in_flight_ask:
                    mark_waiting_permission(controller.runtime, task_id, text, prompt_id)
                else:
                    requeue(controller.runtime, task_id, state="waiting_reply")
            except Exception:
                pass


__all__ = ["process_group_presence", "process_private_receipts"]
