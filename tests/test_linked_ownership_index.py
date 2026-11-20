"""The membership index behind every linked-owner lookup."""
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from companion_v2.conversation.linked_ownership import (LINKS_KEY, link_records_changed, link_for_endpoint,
                                                         link_for_scope)
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import ScopeKind, ScopeRef
from companion_v2.storage.runtime import StorageRuntime

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


class RacingRuntime(Runtime):
    """The host commits a link write while a worker thread is between reading and indexing the record."""

    def __init__(self, value, written):
        super().__init__(value)
        self.written = written

    def read_one(self, sql, parameters):
        row = super().read_one(sql, parameters)
        if self.written is not None:
            self.value, self.written = self.written, None
            link_records_changed(self)
        return row


class LinkIndexTests(unittest.TestCase):
    def test_a_write_committed_during_a_load_is_read_by_the_next_lookup(self):
        runtime = RacingRuntime(record(("qq-main", "100", "200"), ("qq-second", "101", "200")),
                                record(("qq-main", "100", "200")))
        with contextlib.suppress(TaskError):
            link_for_endpoint(runtime, "qq-second", "101", "200")
        self.assertIsNone(link_for_endpoint(runtime, "qq-second", "101", "200"))
        self.assertIsNotNone(link_for_endpoint(runtime, "qq-main", "100", "200"))
        self.assertEqual(runtime.reads, 2)

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


class PinnedSnapshotTests(unittest.TestCase):
    """Only a read of the latest committed state decides the membership every other reader sees.

    A pinned snapshot can be older than the last commit and a thread's own write
    transaction can hold what never commits: lookups there answer from that view only.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = StorageRuntime.open_configuration(data_root=Path(directory.name), installation_id="synthetic")
        self.addCleanup(self.runtime.close)

    def write(self, value):
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (LINKS_KEY, value))
        link_records_changed(self.runtime)

    def test_a_member_detached_while_a_snapshot_was_pinned_is_gone_once_the_reader_exits(self):
        self.write(record(("qq-main", "100", "200"), ("qq-second", "101", "200")))
        self.assertIsNotNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
        with self.runtime.read_snapshot():
            self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (LINKS_KEY,))  # pins it
            # The administrator detaches the member on another thread; the write commits and is announced.
            editor = threading.Thread(target=self.write, args=(record(("qq-main", "100", "200")),))
            editor.start()
            editor.join()
            # Inside the snapshot the reader still sees the membership its snapshot holds.
            self.assertIsNotNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
        self.assertIsNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
        self.assertIsNotNone(link_for_endpoint(self.runtime, "qq-main", "100", "200"))

    def in_thread(self, call):
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("value", call()))
        worker.start()
        worker.join()
        return result["value"]

    def test_a_link_addition_rolled_back_by_its_writer_is_never_seen_by_another_thread(self):
        self.write(record(("qq-main", "100", "200")))
        self.assertIsNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
        with contextlib.suppress(RuntimeError):
            with self.runtime.transaction() as connection:
                connection.execute("UPDATE runtime_metadata SET value = ? WHERE key = ?",
                                   (record(("qq-main", "100", "200"), ("qq-second", "101", "200")), LINKS_KEY))
                link_records_changed(self.runtime)  # announced inside the open transaction, as authority.write does
                # The writer sees its own addition while its transaction is open.
                self.assertIsNotNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
                raise RuntimeError("the edit fails after the write")
        self.assertIsNone(self.in_thread(lambda: link_for_endpoint(self.runtime, "qq-second", "101", "200")))

    def test_a_reader_pinned_before_a_link_existed_sees_no_link_after_another_thread_warmed_the_index(self):
        link_records_changed(self.runtime)  # the host announced the store; no link exists yet
        self.assertIsNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
        with self.runtime.read_snapshot():
            self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (LINKS_KEY,))  # pins it

            def create_and_warm():
                self.write(record(("qq-main", "100", "200"), ("qq-second", "101", "200")))
                return link_for_endpoint(self.runtime, "qq-second", "101", "200")
            self.assertIsNotNone(self.in_thread(create_and_warm))
            self.assertIsNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
        self.assertIsNotNone(link_for_endpoint(self.runtime, "qq-second", "101", "200"))
