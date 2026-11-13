"""Receiver text snapshots derived from an authenticated native input source."""
from dataclasses import dataclass, replace

from companion_v2.execution.models import digest, encode, require
from companion_v2.sources.dependencies import link
from companion_v2.sources.models import SourceRecord
from .quotes.service import quote_reference
from .quotes.models import CapturedQuote


@dataclass(frozen=True)
class NativeTextQuote(CapturedQuote):
    sender_is_self: bool = False
    partial: bool = False
    truncated: bool = False


def reference(parent):
    # One embedded receiver reply per native input, independent of component order.
    return replace(quote_reference(parent, 0), owner="conversation.native_quote")


def publish(messages, context, parent, quote):
    scope = context.scope
    payload = encode(dict(text=quote.text, platform=scope.platform, account_id=scope.account_id,
        chat_id=scope.native_scope_id, message_id=quote.message_id, sender_id=quote.sender_id,
        sender_is_self=quote.sender_is_self, sent_at_unix=quote.sent_at, partial=quote.partial, truncated=quote.truncated,
        attribution="sending_account_only", original_author="unknown"))
    record = messages.sources.prepare_message(SourceRecord(reference(parent), context.audience, digest(payload), payload))
    require(messages.sources.publish(record, observed_at=messages.tasks.now()))
    link(messages.runtime, record.reference, (parent,), messages.tasks.now())


def snapshot(messages, context, parent, at):
    """This input's quote and its governed record, read once; None without one."""
    ref = reference(parent)
    if messages.runtime.read_one("SELECT 1 FROM sources WHERE source_id = ? AND owner = ?",
                                 (ref.source_id, ref.owner)) is None:
        return None
    return ref, messages._read_source(ref, context, "text", at)


def offered(messages, context, parent, at):
    found = snapshot(messages, context, parent, at)
    return {"native-quote:" + found[0].source_id: (found[0], "text", "quoted_message")} if found else {}
