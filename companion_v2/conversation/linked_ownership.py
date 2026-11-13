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
            for member in record["members"] + record.get("detached", [])))))
        self.preferred = next((endpoint for member, endpoint in zip(record["members"], self.endpoints)
                               if member["target_id"] == preferred_target_id(record)), None)

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
               or [row for row in members if row["target_id"] == record["anchor"]["target_id"]] or members)
    return initial[0]["target_id"]


class _Index:
    def __init__(self):
        self.raw, self.rejected, self.members, self.scopes, self.current = None, None, {}, {}, False

    def load(self, runtime):
        row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (LINKS_KEY,))
        raw = row["value"] if row else None
        if raw != self.raw:
            if raw == self.rejected:
                raise ValueError("the stored conversation links are malformed")
            self.rejected = raw
            members, scopes = {}, {}
            for record in (json.loads(raw) if raw else {}).values():
                link = _Link(record)
                members.update(dict.fromkeys(link.endpoints, link))
                scopes.update(dict.fromkeys(link.scopes, link))
            self.raw, self.members, self.scopes = raw, members, scopes
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
        link = link_for_endpoint(runtime, *endpoint)
        if link is not None and scope in link.scopes:
            return link.scope_at(*endpoint)
    return None if joined_principal(principal) else scope


def shared_scopes(runtime, scope):
    link = link_for_scope(runtime, scope)
    return link.scopes if link else (scope,)


def same_conversation(runtime, reader_scope, source_scope):
    return source_scope in shared_scopes(runtime, reader_scope)
