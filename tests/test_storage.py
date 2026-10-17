"""Storage regressions, isolated from credentials, user lists and Instagram."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from instagram_bot import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.follow = self.folder / "Takiplist.txt"
        self.exclude = self.folder / "karalist.txt"
        self.database = self.folder / "statuses.sqlite3"
        self.paths = {
            "TAKIPLIST_PATH": self.follow,
            "KARALIST_PATH": self.exclude,
            "STATUS_DB_PATH": self.database,
            "IDSIFRE_PATH": self.folder / "idsifre.txt",
            "GONDERILER_PATH": self.folder / "gönderiler.txt",
        }
        self.patcher = patch.multiple(storage, **self.paths)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_append_preserves_last_username_when_old_file_has_no_newline(self):
        self.follow.write_text("existing_user", encoding="utf-8")
        result = storage.append_to_takiplist(["new_user", " Existing_User ", "another_user"])
        self.assertEqual(result, ["existing_user", "new_user", "another_user"])
        self.assertEqual(self.follow.read_text(encoding="utf-8"), "existing_user\nnew_user\nanother_user\n")

    def test_atomic_replace_failure_keeps_previous_file_and_cleans_temporary(self):
        self.follow.write_text("original_user\n", encoding="utf-8")
        with patch.object(storage.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                storage.append_to_takiplist(["new_user"])
        self.assertEqual(self.follow.read_text(encoding="utf-8"), "original_user\n")
        self.assertEqual(list(self.folder.iterdir()), [self.follow])

    def test_atomic_flush_failure_does_not_truncate_original(self):
        self.follow.write_text("original_user\n", encoding="utf-8")
        with patch.object(storage.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                storage.save_takiplist(["replacement"])
        self.assertEqual(self.follow.read_text(encoding="utf-8"), "original_user\n")
        self.assertEqual(list(self.folder.iterdir()), [self.follow])

    def test_parallel_appends_do_not_drop_users(self):
        with ThreadPoolExecutor(max_workers=6) as workers:
            list(workers.map(lambda i: storage.append_to_takiplist([f"user_{i}"]), range(30)))
        self.assertEqual(set(storage.read_takiplist()), {f"user_{i}" for i in range(30)})
        self.assertTrue(self.follow.read_bytes().endswith(b"\n"))

    def test_remove_and_exclusion_matching_are_case_insensitive(self):
        storage.save_takiplist(["Third", "First", "second", "FIRST"])
        storage.remove_from_takiplist(" SECOND ")
        storage.write_to_karalist("first")
        storage.write_to_karalist("FIRST")
        storage.temizle_takiplist_karalisteye_gore()
        self.assertEqual(storage.read_takiplist(), ["Third"])
        self.assertEqual(self.exclude.read_text(encoding="utf-8"), "first\n")
        self.assertFalse(self.database.exists())

    def test_missing_status_database_stays_missing_during_read(self):
        self.assertEqual(storage.get_user_statuses(), [])
        self.assertFalse(self.database.exists())

    def test_status_reason_attempts_and_history_survive_module_reload(self):
        storage.record_user_status("Some_User", "following", "attempt_started", increment_attempt=True)
        storage.record_user_status("some_user", "retryable_error", "profile_timeout", "Timeout after navigation")
        storage.record_user_status("SOME_USER", "following", "attempt_started", increment_attempt=True)
        storage.record_user_status("some_user", "followed", "button_confirmed")
        importlib.reload(storage)
        # Reload restores production constants, so restore the isolated paths
        # before invoking any storage operation.
        for name, value in self.paths.items():
            setattr(storage, name, value)
        rows = storage.get_user_statuses()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 2)
        self.assertEqual(rows[0]["status"], "followed")
        self.assertEqual(rows[0]["reason"], "button_confirmed")
        self.assertTrue(rows[0]["updated_at"].endswith("+00:00"))
        with closing(sqlite3.connect(self.database)) as connection:
            events = connection.execute(
                "SELECT status, reason, detail, attempts FROM user_status_events ORDER BY id"
            ).fetchall()
        self.assertEqual(len(events), 4)
        self.assertEqual(events[1], ("retryable_error", "profile_timeout", "Timeout after navigation", 1))

    def test_failed_history_insert_rolls_back_latest_status_and_attempt_count(self):
        storage.record_user_status("user", "pending", "queued")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_event BEFORE INSERT ON user_status_events
                   BEGIN SELECT RAISE(ABORT, 'history unavailable'); END"""
            )
            connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "history unavailable"):
            storage.record_user_status("user", "following", "attempt_started", increment_attempt=True)
        row = storage.get_user_statuses()[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)

    def test_status_search_treats_underscore_as_literal(self):
        storage.record_user_status("a_b", "pending", "queued")
        storage.record_user_status("axb", "pending", "queued")
        self.assertEqual([row["username"] for row in storage.get_user_statuses(search="a_b")], ["a_b"])

    def test_password_login_never_reads_a_file(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("No credential reads")) as read:
            with self.assertRaisesRegex(RuntimeError, "elle giriş"):
                storage.read_idsifre()
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
