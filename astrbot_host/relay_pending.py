"""Small durable queue for delayed group relays and private reply receipts.

V2 keeps these tasks in the same SQLite runtime as the rest of the companion
state.  The queue is intentionally bounded and single-owner: every task belongs
to the authenticated account/scope that created it, and dispatch is performed
from an authenticated inbound event for that same account.
"""

from __future__ import annotations

import json
from time import time
from uuid import uuid4


KEY = "conversation.relay.pending.v1"
MAX_TASKS = 200
LIVE_STATES = frozenset({"waiting", "sending", "waiting_reply", "reporting", "waiting_permission"})
REPORTABLE_KINDS = frozenset({"group_wait", "private_receipt"})


def _read(runtime):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (KEY,))
    if row is None:
        return {"tasks": {}}
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return {"tasks": {}}
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return {"tasks": tasks if isinstance(tasks, dict) else {}}


def _write(connection, data):
    connection.execute(
        "INSERT INTO runtime_metadata(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KEY, json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )


def _remove_task(connection, tasks, task_id):
    task = tasks.pop(task_id)
    if isinstance(task, dict) and task.get("telegram_account") and task.get("kind") == "group_wait":
        outcome = {"sent": "already_sent", "failed": "failed", "waiting": "expired",
                   "expired": "expired"}.get(task.get("state"), "unknown")
        connection.execute("INSERT OR IGNORE INTO runtime_metadata(key,value) VALUES (?,?)",
                           ("conversation.relay.outcome:" + task_id, outcome))


def _prune(connection, data, now):
    tasks = data["tasks"]
    for task_id, task in list(tasks.items()):
        if not isinstance(task, dict):
            _remove_task(connection, tasks, task_id)
            continue
        expires_at = task.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at <= now:
            # 还没结论的待办不直接丢：标记成 expired，等下一次同群事件
            # 把“没等到人、已撤回”回报给发起窗口；其余（已发/已失败）照旧清理。
            if task.get("telegram_account") and task.get("kind") == "group_wait" and task.get("state") == "sending":
                task["state"] = "unknown"
            elif (task.get("state") in LIVE_STATES
                    and task.get("kind") in REPORTABLE_KINDS
                    and not task.get("expired_notice_sent")):
                task["state"] = "expired"
                task["expired_at"] = now
            elif task.get("state") != "expired":
                _remove_task(connection, tasks, task_id)
                continue
        # 过期通知最多保留一天：同群一直没新消息时不至于长期占着队列。
        expired_at = task.get("expired_at")
        if (task.get("state") == "expired" and isinstance(expired_at, (int, float))
                and now - expired_at > 24 * 60 * 60):
            _remove_task(connection, tasks, task_id)
    if len(tasks) > MAX_TASKS:
        ordered = sorted(tasks.items(), key=lambda item: item[1].get("created_at", 0))
        for task_id, _ in ordered[:len(tasks) - MAX_TASKS]:
            _remove_task(connection, tasks, task_id)


def add_task(runtime, task):
    """Add one task, deduplicating an identical live/sent task by signature."""
    now = time()
    with runtime.transaction() as connection:
        data = _read(runtime)
        _prune(connection, data, now)
        signature = task.get("signature", "")
        if signature:
            for existing in data["tasks"].values():
                if existing.get("signature") == signature and existing.get("state") in {
                        "waiting", "sending", "sent", "unknown", "waiting_reply", "reporting",
                        "waiting_permission"}:
                    return existing.get("id", "")
        identifier = uuid4().hex
        record = {**task, "id": identifier, "created_at": now}
        data["tasks"][identifier] = record
        _write(connection, data)
        return identifier


def scheduled_state(runtime, task_id):
    task = _read(runtime)["tasks"].get(task_id) or {}
    if not task:
        row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                               ("conversation.relay.outcome:" + task_id,))
        return row["value"] if row else "unknown"
    state = task.get("state")
    if state == "waiting" and task.get("expires_at", 0) <= time():
        return "expired"
    return {"waiting": "scheduled", "sent": "already_sent", "failed": "failed",
            "expired": "expired"}.get(state, "unknown")


def claim_group(runtime, group_id, target_user_id, now=None, *, telegram_account=None):
    now = time() if now is None else now
    claimed = []
    with runtime.transaction() as connection:
        data = _read(runtime)
        _prune(connection, data, now)
        for task in data["tasks"].values():
            if (task.get("kind") == "group_wait" and task.get("state") == "waiting"
                    and task.get("group_id") == group_id
                    and task.get("telegram_account") == telegram_account
                    and task.get("target_user_id") == target_user_id
                    and task.get("expires_at", 0) > now):
                task["requeue_state"] = "waiting"
                task["state"] = "sending"
                claimed.append(dict(task))
        _write(connection, data)
    return claimed


def _waiting_receipts(data, user_id, account, now, state):
    return sorted((task for task in data["tasks"].values()
                   if task.get("kind") == "private_receipt" and task.get("state") == state
                   and task.get("telegram_account") == account and task.get("target_user_id") == user_id
                   and task.get("expires_at", 0) > now), key=lambda task: task.get("created_at", 0))


def _claim_receipt(tasks):
    if not tasks:
        return []
    task = tasks[0]
    task["requeue_state"] = task["state"]
    task["state"] = "reporting"
    return [dict(task)]


def claim_private_receipts(runtime, target_user_id, now=None, *, permission=False, telegram_account=None):
    now = time() if now is None else now
    with runtime.transaction() as connection:
        data = _read(runtime)
        _prune(connection, data, now)
        state = "waiting_permission" if permission else "waiting_reply"
        claimed = _claim_receipt(_waiting_receipts(data, target_user_id, telegram_account, now, state))
        _write(connection, data)
        return claimed


def claim_telegram_receipt(runtime, account, user_id, message_id, *, reply_to=None, permission_answer=False):
    """One native private message can advance one receipt, including after restart."""
    if type(message_id) is not int or message_id <= 0:
        return [], []
    peer = json.dumps([*account, user_id], separators=(",", ":"))
    # SQLite INTEGER is signed. Each row records 63 independent message IDs.
    block, offset = divmod(message_id, 63)
    block_key = f"conversation.relay.private_seen_block:{peer}:{block}"
    legacy_key = "conversation.relay.private_seen:" + peer
    with runtime.transaction() as connection:
        # Old installations recorded only a high-water mark; keep that replay boundary.
        cursor = connection.execute(
            "INSERT INTO runtime_metadata(key, value) SELECT ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM runtime_metadata WHERE key=? AND CAST(value AS INTEGER)>=?) "
            "ON CONFLICT(key) DO UPDATE SET value=runtime_metadata.value | excluded.value "
            "WHERE (runtime_metadata.value & excluded.value)=0",
            (block_key, 1 << offset, legacy_key, message_id),
        )
        if not cursor.rowcount:
            return [], []
        now = time()
        data = _read(runtime)
        _prune(connection, data, now)
        prompts = _waiting_receipts(data, user_id, account, now, "waiting_permission")
        permission, replies = [], []
        matches = [task for task in prompts if reply_to is not None and task.get("prompt_message_id") == reply_to]
        if matches:
            if len(matches) == 1 and type(reply_to) is int and 0 < reply_to < message_id and permission_answer:
                permission = _claim_receipt(matches)
        else:
            waiting = _waiting_receipts(data, user_id, account, now, "waiting_reply")
            if reply_to is not None:
                addressed = [task for task in waiting if task.get("question_message_id") == reply_to]
                if len(addressed) == 1 and type(reply_to) is int and 0 < reply_to < message_id:
                    replies = _claim_receipt(addressed)
            elif not (prompts and permission_answer):
                eligible = [task for task in waiting if task.get("question_message_id") is None
                            or task["question_message_id"] < message_id]
                if len(eligible) == 1:
                    replies = _claim_receipt(eligible)
        _write(connection, data)
        return permission, replies


def claim_expired(runtime, group_id=None, now=None, *, telegram_account=None):
    """Take expired tasks once so their origin window can be told the relay was dropped.

    Expiry is recorded by ``_prune``; the task is removed here because the notice is
    best-effort and must not be repeated on every later group message.
    """
    now = time() if now is None else now
    claimed = []
    with runtime.transaction() as connection:
        data = _read(runtime)
        _prune(connection, data, now)
        for task_id, task in list(data["tasks"].items()):
            if task.get("state") != "expired" or task.get("telegram_account") != telegram_account:
                continue
            if group_id is not None and (
                    task.get("kind") != "group_wait" or task.get("group_id") != group_id):
                continue
            claimed.append(dict(task))
            _remove_task(connection, data["tasks"], task_id)
        _write(connection, data)
    return claimed


def finish(runtime, task_id, state):
    with runtime.transaction() as connection:
        data = _read(runtime)
        task = data["tasks"].get(task_id)
        if isinstance(task, dict):
            task.pop("requeue_state", None)
            task["state"] = state
        _write(connection, data)


def mark_waiting_permission(runtime, task_id, reply_text, prompt_message_id=None):
    with runtime.transaction() as connection:
        data = _read(runtime)
        task = data["tasks"].get(task_id)
        if isinstance(task, dict):
            task.pop("requeue_state", None)
            task["state"] = "waiting_permission"
            task["reply_text"] = reply_text
            if type(prompt_message_id) is int:
                task["prompt_message_id"] = prompt_message_id
        _write(connection, data)


def requeue(runtime, task_id, state=None):
    with runtime.transaction() as connection:
        data = _read(runtime)
        task = data["tasks"].get(task_id)
        if isinstance(task, dict) and task.get("state") in {"sending", "reporting"}:
            target = state or task.pop("requeue_state", None)
            if target not in {"waiting", "waiting_reply", "waiting_permission"}:
                if task.get("kind") == "group_wait":
                    target = "waiting"
                elif task.get("reply_text"):
                    target = "waiting_permission"
                else:
                    target = "waiting_reply"
            task.pop("requeue_state", None)
            task["state"] = target
        _write(connection, data)


__all__ = ["KEY", "add_task", "claim_group", "claim_private_receipts", "claim_expired", "finish",
           "mark_waiting_permission", "requeue"]
