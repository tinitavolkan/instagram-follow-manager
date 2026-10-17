"""Persistent remaining-user and action history tests in temporary directories."""

import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from instagram_bot.export_lists import ExportLists


try:
    store_module = importlib.import_module("instagram_bot.unfollow_store")
except ModuleNotFoundError:
    store_module = None


def export(account="owner", fingerprint="export-one", following=("alice", "bob", "charlie")):
    return ExportLists(("alice", "outside"), following, ("fixture.json",), account,
                       ("Tarih aralığı doğrulanamadı.",), fingerprint)


class UnfollowStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    def store(self, path=None):
        self.assertIsNotNone(store_module, "Unfollow persistence is not implemented")
        return store_module.UnfollowStore(path or self.path)

    def test_import_computes_candidates_and_returns_detached_snapshot(self):
        store = self.store()
        self.assertFalse(self.path.exists())
        result = store.load_export(export())
        self.assertEqual(result["account"], "owner")
        self.assertEqual((result["followers_count"], result["following_count"], result["mutual_count"], result["candidate_count"]), (2, 3, 1, 2))
        self.assertEqual(result["remaining"], ["bob", "charlie"])
        self.assertEqual(result["sources"], ["fixture.json"])
        self.assertEqual(result["warnings"], ["Tarih aralığı doğrulanamadı."])
        self.assertTrue(result["imported_at"])
        self.assertEqual((result["history"], result["last_results"], result["in_progress"]), ([], {}, ""))
        result["remaining"].clear()
        result["sources"].append("changed")
        self.assertEqual(store.snapshot()["remaining"], ["bob", "charlie"])
        self.assertEqual(store.snapshot()["sources"], ["fixture.json"])

    def test_only_confirmed_or_explicitly_skipped_statuses_remove_a_candidate(self):
        for status in ("unfollowed", "already_not_following", "unavailable", "error", "unconfirmed", "restricted", "stopped"):
            with self.subTest(status=status):
                store = self.store(self.path.with_name(status + ".json"))
                store.load_export(export())
                store.begin("bob")
                result = store.finish("bob", status, "result detail", submitted=True, target_verified=status == 'unavailable')
                expected = ["charlie"] if status in {"unfollowed", "already_not_following", "unavailable"} else ["bob", "charlie"]
                self.assertEqual(result["remaining"], expected)
                self.assertEqual(result["in_progress"], "")
                record = result["history"][0]
                self.assertEqual((record["username"], record["status"], record["detail"], record["submitted"]), ("bob", status, "result detail", True))
                self.assertTrue(record["at"])
                self.assertEqual(result["last_results"]["bob"], record)

    def test_unavailable_skip_survives_restart_and_same_export_reimport(self):
        store = self.store()
        store.load_export(export())
        store.begin('bob')
        store.finish('bob', 'unavailable', 'Profile ulaşılamadı.', submitted=False, target_verified=True)
        restored = self.store()
        state = restored.load_export(export())
        self.assertEqual(state['remaining'], ['charlie'])
        self.assertEqual(state['history'][0]['status'], 'unavailable')
        self.assertFalse(state['history'][0]['submitted'])

    def test_legacy_unverified_unavailable_skips_are_restored_once(self):
        store = self.store()
        store.load_export(export())
        store.begin('bob')
        store.finish('bob', 'unavailable', 'Eski hedef doğrulanmadı.', submitted=False)
        restored = self.store()
        state = restored.snapshot()
        self.assertEqual(state['remaining'], ['bob', 'charlie'])
        self.assertEqual(state['history'][0]['status'], 'error')
        self.assertEqual(state['history'][1]['status'], 'unavailable')
        self.assertEqual(self.store().snapshot()['history'], state['history'])

    def test_begin_is_on_disk_before_it_returns_and_reopen_recovers_as_unconfirmed(self):
        store = self.store()
        store.load_export(export())
        store.begin("bob")
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["accounts"]["owner"]["in_progress"], "bob")
        recovered = self.store().snapshot()
        self.assertEqual(recovered["remaining"], ["bob", "charlie"])
        self.assertEqual(recovered["in_progress"], "")
        self.assertEqual((recovered["history"][0]["username"], recovered["history"][0]["status"]), ("bob", "unconfirmed"))
        self.assertEqual(len(self.store().snapshot()["history"]), 1)

    def test_same_export_reimport_does_not_restore_completed_candidates(self):
        store = self.store()
        store.load_export(export())
        store.begin("bob")
        store.finish("bob", "unfollowed")
        store = self.store()
        result = store.load_export(export())
        self.assertEqual(result["remaining"], ["charlie"])
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(result["last_results"]["bob"]["status"], "unfollowed")

    def test_different_accounts_keep_separate_completed_users_and_history(self):
        store = self.store()
        store.load_export(export("owner"))
        store.begin("bob")
        store.finish("bob", "unfollowed")
        other = store.load_export(export("other"))
        self.assertEqual(other["account"], "other")
        self.assertEqual(other["remaining"], ["bob", "charlie"])
        self.assertEqual(other["history"], [])
        store.begin("charlie")
        store.finish("charlie", "already_not_following")
        owner = store.load_export(export("owner"))
        self.assertEqual(owner["remaining"], ["charlie"])
        self.assertEqual([item["username"] for item in owner["history"]], ["bob"])

    def test_new_export_keeps_history_and_uses_its_own_completion_scope(self):
        store = self.store()
        store.load_export(export())
        store.begin("bob")
        store.finish("bob", "unfollowed")
        result = store.load_export(export(fingerprint="export-two", following=("alice", "bob", "dave")))
        self.assertEqual(result["remaining"], ["bob", "dave"])
        self.assertEqual(result["history"][0]["username"], "bob")
        result = store.load_export(export())
        self.assertEqual(result["remaining"], ["charlie"])

    def test_account_validation_and_hint_conflict_preserve_existing_state(self):
        store = self.store()
        store.load_export(export())
        original = self.path.read_bytes()
        for account in ("other", "bad/name", "bad name", "x" * 31):
            with self.subTest(account=account):
                with self.assertRaises(ValueError):
                    store.load_export(export(), account=account)
                self.assertEqual(self.path.read_bytes(), original)
        clean = store.load_export(export(""), account=" @OWNER ")
        self.assertEqual(clean["account"], "owner")
        with self.assertRaises(ValueError):
            store.load_export(export(""))

    def test_begin_rejects_mutual_unknown_completed_and_parallel_actions(self):
        store = self.store()
        store.load_export(export())
        for username in ("alice", "missing", "bad/name"):
            with self.subTest(username=username):
                with self.assertRaises(ValueError):
                    store.begin(username)
        store.begin("bob")
        with self.assertRaises(ValueError):
            store.begin("charlie")
        with self.assertRaises(ValueError):
            store.load_export(export("other"))
        store.finish("bob", "unfollowed")
        with self.assertRaises(ValueError):
            store.begin("bob")

    def test_finish_requires_matching_pending_action_and_known_status(self):
        store = self.store()
        store.load_export(export())
        with self.assertRaises(ValueError):
            store.finish("bob", "unfollowed")
        store.begin("bob")
        for username, status in (("charlie", "unfollowed"), ("bob", "success")):
            with self.subTest(username=username, status=status):
                with self.assertRaises(ValueError):
                    store.finish(username, status)
        self.assertEqual(store.snapshot()["in_progress"], "bob")
        self.assertEqual(store.snapshot()["remaining"], ["bob", "charlie"])

    def test_history_is_newest_first_and_last_results_tracks_latest_attempt(self):
        store = self.store()
        store.load_export(export())
        store.begin("bob")
        store.finish("bob", "unconfirmed", "first")
        store.begin("charlie")
        store.finish("charlie", "stopped", "second")
        store.begin("bob")
        result = store.finish("bob", "unfollowed", "third")
        self.assertEqual([entry["detail"] for entry in result["history"]], ["third", "second", "first"])
        self.assertEqual(result["last_results"]["bob"]["detail"], "third")

    def test_failed_atomic_replace_does_not_claim_begin_or_damage_saved_state(self):
        store = self.store()
        store.load_export(export())
        original = self.path.read_bytes()
        with patch.object(store_module.os, "replace", side_effect=OSError("disk busy")):
            with self.assertRaises(OSError):
                store.begin("bob")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(store.snapshot()["in_progress"], "")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupt_state_is_not_silently_replaced(self):
        self.path.write_text('{"bad":', encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store()
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_finish_keeps_pending_and_does_not_claim_terminal_success(self):
        store = self.store()
        store.load_export(export())
        store.begin("bob")
        original = self.path.read_bytes()
        with patch.object(store_module.os, "replace", side_effect=OSError("disk busy")):
            with self.assertRaises(OSError):
                store.finish("bob", "unfollowed", submitted=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(store.snapshot()["in_progress"], "bob")
        self.assertEqual(store.snapshot()["remaining"], ["bob", "charlie"])
        self.assertEqual(store.snapshot()["history"], [])
        recovered = self.store().snapshot()
        self.assertEqual(recovered["remaining"], ["bob", "charlie"])
        self.assertEqual(recovered["history"][0]["status"], "unconfirmed")

    def test_nontext_account_is_a_validation_error_before_mutation(self):
        store = self.store()
        store.load_export(export())
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            store.load_export(export(), account=0)
        self.assertEqual(self.path.read_bytes(), original)
    def test_nontext_status_is_a_validation_error_without_clearing_pending(self):
        store = self.store()
        store.load_export(export())
        store.begin("bob")
        with self.assertRaises(ValueError):
            store.finish("bob", [])
        self.assertEqual(store.snapshot()["in_progress"], "bob")

    def test_semantically_damaged_completed_or_history_state_is_not_reset(self):
        store = self.store()
        store.load_export(export())
        store.begin("bob")
        store.finish("bob", "unfollowed")
        valid = json.loads(self.path.read_text(encoding="utf-8"))
        for field, value in (("completed", []), ("last_results", {}), ("history", "lost")):
            with self.subTest(field=field):
                damaged = json.loads(json.dumps(valid))
                damaged["accounts"]["owner"][field] = value
                self.path.write_text(json.dumps(damaged), encoding="utf-8")
                original = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store()
                self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
