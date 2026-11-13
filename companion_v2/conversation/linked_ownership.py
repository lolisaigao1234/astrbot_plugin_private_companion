"""Resolve stored conversation owners without borrowing a live endpoint.

The host owns link management. Core readers consume only its persisted anchor;
physical scopes remain the authority, delivery and source-provenance addresses.
"""
from dataclasses import replace
import json
from weakref import WeakKeyDictionary, ref

from companion_v2.foundation.refs import ScopeKind, ScopeRef

LINKS_KEY = "astrbot.private-conversation-links.v1"
PRIVATE_HOST_ID = "astrbot-native-linked-private"

_INDEXES = WeakKeyDictionary()
_RECORD_RUNTIMES = WeakKeyDictionary()


class _Link(dict):
    """One stored link with its anchor and member scopes restored once."""

    def __init__(self, record):
        super().__init__(record)
        self.anchor = ScopeRef.restore(record["anchor"]["scope"])
        self.role_id = record["anchor"]["role"]["subject_id"]
        self.endpoints = tuple((member["platform_id"], member["account_id"], member["participant_id"])
                               for member in record["members"])
        self.scopes = tuple(dict.fromkeys((self.anchor, *(self.scope_at(member["platform_id"],
            member["account_id"], member["participant_id"])
            # An endpoint that left, also for a conversation of its own, wrote records here.
            for member in record["members"] + record.get("detached", []) + record.get("former", [])))))
        wanted = preferred_target_id(record)
        self.preferred = next((endpoint for member, endpoint in zip(record["members"], self.endpoints)
                               if wanted is not None and member.get("target_id") == wanted), None)

    def scope_at(self, platform, account, participant):
        return replace(self.anchor, platform=platform, account_id=account,
                       native_scope_id=participant, session_id=participant)


def preferred_target_id(record):
    """The member that receives the conversation's reminders and proactive text.

    Chosen by the administrator. A link that has never named one starts with its
    Telegram member, else the endpoint it was created on, else its first member.
    A named target that left the link stays named: the conversation then has no
    destination until a member is chosen.
    """
    if "preferred_target_id" in record:
        return record["preferred_target_id"]
    members = record["members"]
    initial = ([row for row in members if row.get("platform_name") == "telegram"]
               or [row for row in members if row.get("target_id") == record["anchor"].get("target_id")] or members)
    return initial[0].get("target_id") if initial else None


class _Index:
    def __init__(self):
        self.raw, self.rejected, self.members, self.scopes, self.removed, self.current = None, None, {}, {}, set(), False

    def load(self, runtime):
        row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (LINKS_KEY,))
        raw = row["value"] if row else None
        if raw != self.raw:
            if raw == self.rejected:
                raise ValueError("the stored conversation links are malformed")
            self.rejected = raw
            members, scopes, removed = {}, {}, set()
            for record in (json.loads(raw) if raw else {}).values():
                link = _Link(record)
                members.update(dict.fromkeys(link.endpoints, link))
                scopes.update(dict.fromkeys(link.scopes, link))
                removed.update((row["platform_id"], row["account_id"], row["participant_id"])
                               for row in record.get("detached", []))
            self.raw, self.members, self.scopes, self.removed = raw, members, scopes, removed - set(members)
        self.current = True


def _records_runtime(runtime):
    source = _RECORD_RUNTIMES.get(runtime)
    return source() if source is not None else runtime


def serve_link_records(store, runtime):
    """Settings owners built on the configuration side store read the links of its main runtime."""
    _RECORD_RUNTIMES[store] = ref(runtime)


def link_records_changed(runtime):
    """The host that manages links announces its store, and every write of the record.

    A store no host has announced has no links and is never queried for them.
    """
    _INDEXES.setdefault(_records_runtime(runtime), _Index()).current = False


def _lookup(runtime, table, key):
    runtime = _records_runtime(runtime)
    index = _INDEXES.get(runtime)
    if index is None:
        return None
    if not index.current:
        try:
            index.load(runtime)
        except Exception:  # noqa: BLE001 - any unreadable record keeps only its last known members closed
            pass
    link = getattr(index, table).get(key)
    if link is not None and not index.current:
        from companion_v2.execution.models import TaskError
        raise TaskError("conversation_link_unavailable", "the linked conversation's ownership cannot be established")
    return link


def link_for_endpoint(runtime, platform, account, participant):
    """The link this physical endpoint is a member of.

    The record is read after the host announced it or wrote it; in between a
    lookup costs no query. An endpoint in no link never depends on the record
    being readable; a known member stays closed while it is not.
    """
    return _lookup(runtime, "members", (platform, account, participant))


def conversation_link(runtime, scope):
    """The link whose conversation this scope belongs to, also once its endpoint has left.

    Work recorded on any scope of a linked conversation is the conversation's
    work: where it is delivered is the link's decision, never the stored scope's.
    """
    return _lookup(runtime, "scopes", scope) if scope.kind is ScopeKind.PRIVATE else None


def endpoint_removed(runtime, platform, account, participant):
    """An endpoint taken out of a link stays closed until it is admitted on its own.

    Its native keys can be the shared owner's (the endpoint the conversation
    started on), so membership, never the key, decides what it may read.
    """
    index = _INDEXES.get(_records_runtime(runtime))
    if index is None:
        return False
    link_for_endpoint(runtime, platform, account, participant)
    return (platform, account, participant) in index.removed


def link_for_scope(runtime, scope):
    if scope.kind is not ScopeKind.PRIVATE:
        return None
    link = link_for_endpoint(runtime, scope.platform, scope.account_id, scope.native_scope_id)
    if link and scope.conversation_id == link.anchor.conversation_id and scope.bot_id == link.anchor.bot_id:
        return link
    return None


def owner_scope(runtime, scope):
    link = link_for_scope(runtime, scope)
    return link.anchor if link else scope


def record_owner_scope(runtime, scope):
    """The owner of a stored record: the conversation its scope was written for.

    Resolved from the link's persisted scopes, which keep an endpoint that has
    left, so a record stays the conversation's whichever member is removed
    afterwards. Who may act on it is decided on the acting endpoint.
    """
    link = conversation_link(runtime, scope)
    return link.anchor if link else scope


def joined_principal(principal):
    """Private link identities never inherit subject-level administrator tiers."""
    return principal.host_id == PRIVATE_HOST_ID


def endpoint_scope(runtime, principal, scope):
    """The scope whose grants decide what this principal may do on ``scope``.

    Linked endpoints share records, never authority: inside its link a member
    answers with its own endpoint's grants, whichever member's scope a shared
    record names. A joined principal exists only for its current link, so any
    other scope is refused (``None``) before a grant is looked at.
    """
    if scope.kind is ScopeKind.PRIVATE:
        endpoint = (principal.platform, principal.account_id, principal.native_account_id)
        if endpoint_removed(runtime, *endpoint):
            return None
        link = link_for_endpoint(runtime, *endpoint)
        if link is not None and scope in link.scopes:
            return link.scope_at(*endpoint)
    return None if joined_principal(principal) else scope


def shared_scopes(runtime, scope):
    link = link_for_scope(runtime, scope)
    return link.scopes if link else (scope,)


def same_conversation(runtime, reader_scope, source_scope):
    return source_scope in shared_scopes(runtime, reader_scope)


def conversation_windows(runtime, context):
    """The context keys under which a linked conversation's background work is recorded.

    ``None`` outside a link: one window, read as it always was. A member reads,
    in place, what was recorded for the other scopes of its link as well, each
    under the window of that scope's latest turn; nothing is copied or rekeyed.
    """
    link = link_for_scope(runtime, context.scope)
    if link is None:
        return None
    from companion_v2.conversation.native_turns import latest_scope
    latest = (latest_scope(runtime, scope) for scope in link.scopes if scope != context.scope)
    return tuple(dict.fromkeys((context.key, *(row["context_key"] for row in latest if row is not None))))


def window_filter(runtime, context, column="origin_key"):
    """One row predicate and its one value: this window, or its linked conversation's windows."""
    keys = conversation_windows(runtime, context)
    if keys is None:
        return column + " = ?", context.key
    return column + " IN (SELECT value FROM json_each(?))", json.dumps(keys)
