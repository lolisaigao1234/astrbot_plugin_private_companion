"""Shared installation-core check for chat commands that change shared state."""

from __future__ import annotations

from companion_v2.identity_policy.models import AuthorizationTier
from companion_v2.identity_policy.object_access import access_tier


def is_core_subject(runtime, subject):
    if runtime is None or subject is None:
        return False
    return access_tier(runtime, subject) is AuthorizationTier.CORE
