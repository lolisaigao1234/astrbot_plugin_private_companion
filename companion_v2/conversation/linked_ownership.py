"""Resolve stored conversation owners without borrowing a live endpoint.

The host owns link management. Core readers consume only its persisted anchor;
physical scopes remain the authority, delivery and source-provenance addresses.
"""
import json
from dataclasses import replace

from companion_v2.foundation.refs import ScopeKind, ScopeRef

LINKS_KEY = "astrbot.private-conversation-links.v1"
PRIVATE_HOST_ID = "astrbot-native-linked-private"


def link_for_endpoint(runtime, platform, account, participant):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (LINKS_KEY,))
    return next((link for link in (json.loads(row["value"]) if row else {}).values()
                 if any((member["platform_id"], member["account_id"], member["participant_id"])
                        == (platform, account, participant) for member in link["members"])), None)


def link_for_scope(runtime, scope):
    if scope.kind is not ScopeKind.PRIVATE:
        return None
    link = link_for_endpoint(runtime, scope.platform, scope.account_id, scope.native_scope_id)
    if link:
        anchor = ScopeRef.restore(link["anchor"]["scope"])
        if scope.conversation_id == anchor.conversation_id and scope.bot_id == anchor.bot_id:
            return link
    return None


def owner_scope(runtime, scope):
    link = link_for_scope(runtime, scope)
    return ScopeRef.restore(link["anchor"]["scope"]) if link else scope


def with_owner(runtime, context):
    scope = owner_scope(runtime, context.scope)
    return replace(context, storage_scope=scope if scope != context.scope else None)


def joined_principal(principal):
    """Private link identities never inherit subject-level administrator tiers."""
    return principal.host_id == PRIVATE_HOST_ID


def scopes_for_link(link):
    anchor = ScopeRef.restore(link["anchor"]["scope"])
    return tuple(dict.fromkeys((anchor, *(replace(anchor, platform=member["platform_id"],
        account_id=member["account_id"], native_scope_id=member["participant_id"], session_id=member["participant_id"])
        for member in link["members"] + link.get("detached", [])))))


def action_scope(runtime, principal, scope):
    """Shared-source reads still require the requesting endpoint's own action."""
    if not scope.is_resolved:
        return scope
    link = link_for_endpoint(runtime, principal.platform, principal.account_id, principal.native_account_id)
    if link and scope in scopes_for_link(link):
        return replace(ScopeRef.restore(link["anchor"]["scope"]), platform=principal.platform,
            account_id=principal.account_id, native_scope_id=principal.native_account_id,
            session_id=principal.native_account_id)
    return scope


def shared_scopes(runtime, scope):
    link = link_for_scope(runtime, scope)
    return scopes_for_link(link) if link else (scope,)


def same_conversation(runtime, reader_scope, source_scope):
    return source_scope in shared_scopes(runtime, reader_scope)
