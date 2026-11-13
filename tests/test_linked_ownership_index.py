"""The membership index behind every linked-owner lookup."""
import json
import unittest

from companion_v2.conversation.linked_ownership import (LINKS_KEY, link_records_changed, link_for_endpoint,
                                                         link_for_scope)
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import ScopeKind, ScopeRef

ANCHOR = ScopeRef("synthetic", "qq-main", ScopeKind.PRIVATE, "200", "bot-a", "100", "conversation-a", "200")


def record(*members):
    return json.dumps({"owner-dialogue": {"schema_version": 1, "link_id": "owner-dialogue", "revision": 1,
        "conversation_id": "conversation-a",
        "anchor": {"scope": ANCHOR.document(), "role": {"subject_id": "role-a"}},
        "members": [dict(platform_id=platform, account_id=account, participant_id=participant)
                    for platform, account, participant in members]}})


class Runtime:
    """Only the one metadata read the index performs."""

    def __init__(self, value=None, *, announced=True):
        self.value, self.failure, self.reads = value, None, 0
        if announced:
            link_records_changed(self)

    def read_one(self, sql, parameters):
        assert parameters == (LINKS_KEY,)
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        return None if self.value is None else {"value": self.value}


class LinkIndexTests(unittest.TestCase):
    def test_the_record_is_read_once_and_again_only_after_the_host_wrote_it(self):
        runtime = Runtime(record(("qq-main", "100", "200"), ("qq-second", "101", "200")))
        first = link_for_endpoint(runtime, "qq-second", "101", "200")
        self.assertEqual(first["link_id"], "owner-dialogue")
        self.assertIs(link_for_endpoint(runtime, "qq-main", "100", "200"), first)
        self.assertIsNone(link_for_endpoint(runtime, "qq-second", "101", "999"))
        self.assertEqual(link_for_scope(runtime, ANCHOR).anchor, ANCHOR)
        self.assertEqual(runtime.reads, 1)
        runtime.value = record(("qq-second", "101", "200"))
        link_records_changed(runtime)
        self.assertIsNone(link_for_endpoint(runtime, "qq-main", "100", "200"))
        self.assertIsNot(link_for_endpoint(runtime, "qq-second", "101", "200"), first)
        self.assertEqual(runtime.reads, 2)

    def test_no_record_means_no_link_for_any_endpoint_without_further_reads(self):
        runtime = Runtime()
        for _ in range(3):
            self.assertIsNone(link_for_endpoint(runtime, "qq-main", "100", "200"))
            self.assertIsNone(link_for_scope(runtime, ANCHOR))
        self.assertEqual(runtime.reads, 1)

    def test_a_store_no_host_announced_is_never_queried(self):
        runtime = Runtime(record(("qq-main", "100", "200")), announced=False)
        self.assertIsNone(link_for_endpoint(runtime, "qq-main", "100", "200"))
        self.assertEqual(runtime.reads, 0)

    def test_an_unreadable_record_closes_known_members_and_leaves_other_endpoints_unlinked(self):
        import sqlite3
        for broken in ("{not json", json.dumps({"owner-dialogue": {"members": [{}]}}), json.dumps([1])):
            with self.subTest(broken=broken):
                runtime = Runtime(record(("qq-main", "100", "200")))
                self.assertIsNotNone(link_for_endpoint(runtime, "qq-main", "100", "200"))
                runtime.value = broken
                link_records_changed(runtime)
                self.assertIsNone(link_for_endpoint(runtime, "qq-second", "101", "200"))
                with self.assertRaises(TaskError) as closed:
                    link_for_endpoint(runtime, "qq-main", "100", "200")
                self.assertEqual(closed.exception.code, "conversation_link_unavailable")
        runtime = Runtime(record(("qq-main", "100", "200")))
        link_for_endpoint(runtime, "qq-main", "100", "200")
        runtime.failure = sqlite3.OperationalError("database is locked")
        link_records_changed(runtime)
        self.assertIsNone(link_for_endpoint(runtime, "qq-second", "101", "200"))
        with self.assertRaises(TaskError):
            link_for_scope(runtime, ANCHOR)
        runtime.failure = None
        self.assertEqual(link_for_scope(runtime, ANCHOR).anchor, ANCHOR)

    def test_a_record_that_was_never_readable_does_not_disturb_unlinked_endpoints(self):
        runtime = Runtime("{not json")
        self.assertIsNone(link_for_endpoint(runtime, "qq-main", "100", "200"))
        self.assertIsNone(link_for_endpoint(runtime, "qq-main", "100", "200"))
