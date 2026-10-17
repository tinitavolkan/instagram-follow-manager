"""Instagram export parsing with temporary, synthetic local files only."""

from dataclasses import FrozenInstanceError
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


try:
    exports = importlib.import_module("instagram_bot.export_lists")
except ModuleNotFoundError:
    exports = None


def follower(name, href=None):
    return {"title": "", "string_list_data": [{"value": name,
        "href": href or f"https://www.instagram.com/{name.lstrip('@')}/", "timestamp": 1700000000}]}


def following(name, href=None):
    return {"title": name, "string_list_data": [{
        "href": href or f"https://www.instagram.com/_u/{name.lstrip('@')}", "timestamp": 1700000000}]}


def html_list(kind, *names):
    rows = []
    for name in names:
        heading = f"<h2>{name}</h2>" if kind == "following" else ""
        prefix = "_u/" if kind == "following" else ""
        rows.append(f'<div>{heading}<div><a href="https://www.instagram.com/{prefix}{name}/">{name}</a></div></div>')
    return f"<!doctype html><html><head><title>{kind.title()}</title></head><body><h1>{kind.title()}</h1>{''.join(rows)}</body></html>"


class ExportListsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_json(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def write_html(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def load(self, paths):
        self.assertIsNotNone(exports, "Export import is not implemented")
        return exports.load_exports(paths)

    def test_json_numbered_followers_and_new_following_schema_normalize_names(self):
        first = self.write_json("followers_1.json", [follower("@Alice", "https://www.instagram.com/ALICE/")])
        second = self.write_json("followers_2.json", [follower("BOB"), follower("alice")])
        chosen = self.write_json("following.json", {"relationships_following": [following("alice"), following("DAVE")]})
        result = self.load([first, second, chosen])
        self.assertEqual(result.followers, ("alice", "bob"))
        self.assertEqual(result.following, ("alice", "dave"))
        self.assertEqual(set(result.following) - set(result.followers), {"dave"})
        self.assertEqual(len(result.sources), 3)
        self.assertTrue(result.fingerprint)
        self.assertTrue(any("tarih" in warning.casefold() for warning in result.warnings))
        with self.assertRaises(FrozenInstanceError):
            result.followers = ()

    def test_json_html_mixed_pair_reads_h2_and_u_profile_urls(self):
        followers = self.write_json("followers_1.json", [follower("alice")])
        following = self.write_html("following.html", html_list("following", "alice", "bob"))
        result = self.load([followers, following])
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.following, ("alice", "bob"))

    def test_zip_reads_only_relationship_lists_without_extracting_or_changing_files(self):
        archive = self.root / "instagram-owner-2026-10-16-test.zip"
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("connections/followers_and_following/followers_1.json", json.dumps([follower("alice")]))
            zipped.writestr("connections/followers_and_following/following.html", html_list("following", "bob"))
            zipped.writestr("connections/followers_and_following/pending_follow_requests.json", "invalid ignored JSON")
            zipped.writestr("threads/followers_1.json", "invalid ignored Threads JSON")
            zipped.writestr("connections/close_friends.json", "invalid ignored JSON")
        original = archive.read_bytes()
        result = self.load([archive])
        self.assertEqual(result.account_hint, "owner")
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.following, ("bob",))
        self.assertEqual(len(result.sources), 2)
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [archive.name])

    def test_missing_numbered_part_and_missing_list_fail_closed(self):
        first = self.write_json("followers_1.json", [follower("alice")])
        third = self.write_json("followers_3.json", [follower("bob")])
        chosen = self.write_json("following.json", {"relationships_following": [following("bob")]})
        self.assertIsNotNone(exports, "Export import is not implemented")
        with self.assertRaises(exports.ExportImportError):
            self.load([first, third, chosen])
        with self.assertRaises(exports.ExportImportError):
            self.load([first])

    def test_conflicting_names_inside_one_record_are_rejected(self):
        followers = self.write_json("followers_1.json", [follower("alice", "https://www.instagram.com/bob/")])
        chosen = self.write_json("following.json", {"relationships_following": [following("bob")]})
        self.assertIsNotNone(exports, "Export import is not implemented")
        with self.assertRaises(exports.ExportImportError):
            self.load([followers, chosen])

    def test_different_account_hints_are_rejected(self):
        followers = self.write_json("instagram-owner-2026-10-16-a/followers_1.json", [follower("alice")])
        chosen = self.write_json("instagram-other-2026-10-16-b/following.json", {"relationships_following": [following("bob")]})
        self.assertIsNotNone(exports, "Export import is not implemented")
        with self.assertRaises(exports.ExportImportError):
            self.load([followers, chosen])

    def test_folder_and_duplicate_format_parts_merge_once_and_ignore_neighbors(self):
        root = self.root / "instagram-owner-2026-10-16-export"
        first = self.write_json(str(root.relative_to(self.root) / "connections/followers_1.json"), [follower("Alice")])
        self.write_html(str(root.relative_to(self.root) / "connections/followers_1.html"), html_list("followers", "alice"))
        self.write_json(str(root.relative_to(self.root) / "connections/followers_2.json"), [follower("bob")])
        self.write_json(str(root.relative_to(self.root) / "connections/following.json"), {"relationships_following": [following("carol")]})
        self.write_json(str(root.relative_to(self.root) / "threads/followers_1.json"), {"unread": "not Instagram"})
        self.write_json(str(root.relative_to(self.root) / "connections/blocked_profiles.json"), {"unread": "not a list"})
        result = self.load([root, first])
        self.assertEqual(result.account_hint, "owner")
        self.assertEqual(result.followers, ("alice", "bob"))
        self.assertEqual(result.following, ("carol",))
        self.assertEqual(len(result.sources), 4)
        self.assertEqual(self.load([root]).fingerprint, result.fingerprint)

    def test_html_only_and_h2_only_following_are_supported(self):
        followers = self.write_html("followers_1.html", html_list("followers", "Alice"))
        chosen = self.write_html("following.html", "<html><title>Following</title><h1>Following</h1><div><h2>ALICE</h2></div><div><h2>bob</h2></div></html>")
        result = self.load([followers, chosen])
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.following, ("alice", "bob"))

    def test_html_user_heading_does_not_rescue_a_foreign_profile_link(self):
        followers = self.write_json("followers_1.json", [follower("alice")])
        chosen = self.write_html("following.html", '<html><h1>Following</h1><div><h2>bob</h2><a href="https://threads.net/@bob">bob</a></div></html>')
        with self.assertRaises(exports.ExportImportError):
            self.load([followers, chosen])

    def test_html_row_heading_and_href_must_agree_even_when_global_sets_match(self):
        followers = self.write_json("followers_1.json", [follower("alice")])
        chosen = self.write_html("following.html", '<html><h1>Following</h1><div><h2>alice</h2><a href="https://www.instagram.com/bob/">bob</a></div><div><h2>bob</h2><a href="https://www.instagram.com/alice/">alice</a></div></html>')
        with self.assertRaises(exports.ExportImportError):
            self.load([followers, chosen])

    def test_empty_lists_are_valid_but_unknown_json_schema_is_not(self):
        followers = self.write_json("followers_1.json", [])
        chosen = self.write_json("following.json", {"relationships_following": []})
        result = self.load([followers, chosen])
        self.assertEqual((result.followers, result.following), ((), ()))
        self.write_json("following.json", {"relationships_follow_requests_sent": []})
        with self.assertRaises(exports.ExportImportError):
            self.load([followers, chosen])

    def test_value_can_be_an_instagram_url_but_invalid_names_and_nontext_titles_fail(self):
        first = self.write_json("followers_1.json", [follower("https://www.instagram.com/_u/ALICE", "https://www.instagram.com/alice/")])
        chosen = self.write_json("following.json", {"relationships_following": [following("@Alice", "https://www.instagram.com/_u/alice")]})
        self.assertEqual(self.load([first, chosen]).followers, ("alice",))
        for bad in ("bad name", "bad-name", "x" * 31, "@@alice"):
            with self.subTest(bad=bad):
                self.write_json("followers_1.json", [follower(bad)])
                with self.assertRaises(exports.ExportImportError):
                    self.load([first, chosen])
        malformed = follower("alice")
        malformed["title"] = 0
        self.write_json("followers_1.json", [malformed])
        with self.assertRaises(exports.ExportImportError):
            self.load([first, chosen])

    def test_missing_first_part_and_conflicting_duplicate_parts_fail(self):
        second = self.write_json("followers_2.json", [follower("alice")])
        chosen = self.write_json("following.json", {"relationships_following": []})
        with self.assertRaises(exports.ExportImportError):
            self.load([second, chosen])
        first = self.write_json("followers_1.json", [follower("alice")])
        alternate = self.write_html("followers_1.html", html_list("followers", "bob"))
        with self.assertRaises(exports.ExportImportError):
            self.load([first, alternate, chosen])

    def test_archive_traversal_and_size_limits_fail_without_extraction(self):
        archive = self.root / "unsafe.zip"
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("../followers_1.json", "[]")
            zipped.writestr("following.json", '{"relationships_following":[]}')
        with self.assertRaises(exports.ExportImportError):
            self.load([archive])
        first = self.write_json("followers_1.json", [follower("alice")])
        chosen = self.write_json("following.json", {"relationships_following": []})
        with patch.object(exports, "_MAX_FILE_BYTES", 16):
            with self.assertRaises(exports.ExportImportError):
                self.load([first, chosen])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["followers_1.json", "following.json", "unsafe.zip"])

    def test_malformed_json_and_utf8_fail_instead_of_returning_an_empty_list(self):
        first = self.write_json("followers_1.json", [follower("alice")])
        chosen = self.write_json("following.json", {"relationships_following": []})
        for raw in (b"{not json}", b"\xff\xfe"):
            with self.subTest(raw=raw):
                first.write_bytes(raw)
                with self.assertRaises(exports.ExportImportError):
                    self.load([first, chosen])

    def test_account_or_folder_name_containing_threads_is_not_a_threads_export(self):
        folder = "instagram-threads_owner-2026-10-16-export"
        first = self.write_json(folder + "/followers_1.json", [follower("alice")])
        chosen = self.write_json(folder + "/following.json", {"relationships_following": [following("bob")]})
        result = self.load([first, chosen])
        self.assertEqual(result.account_hint, "threads_owner")
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.following, ("bob",))

    def test_unreasonable_part_number_is_rejected_without_allocating_a_huge_gap(self):
        first = self.write_json("followers_" + "9" * 30 + ".json", [follower("alice")])
        chosen = self.write_json("following.json", {"relationships_following": []})
        with self.assertRaises(exports.ExportImportError):
            self.load([first, chosen])

    def test_same_payload_reimport_from_unhinted_zip_keeps_completed_user_removed(self):
        from instagram_bot.unfollow_store import UnfollowStore

        prefix = "instagram-owner-2026-10-16-original/"
        followers = self.write_json(prefix + "followers_1.json", [follower("alice")])
        chosen = self.write_json(prefix + "following.json", {"relationships_following": [following("bob")]})
        original = self.load([followers, chosen])
        archive = self.root / "generic-export.zip"
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("followers_1.json", followers.read_bytes())
            zipped.writestr("following.json", chosen.read_bytes())
        moved = self.load([archive])
        self.assertEqual((original.account_hint, moved.account_hint), ("owner", ""))
        store = UnfollowStore(self.root / "state.json")
        store.load_export(original)
        store.begin("bob")
        store.finish("bob", "unfollowed", submitted=True)
        reimported = store.load_export(moved, account="owner")
        self.assertEqual(reimported["remaining"], [],
                         f"same payload changed fingerprint: {original.fingerprint} -> {moved.fingerprint}")
        self.assertEqual(len(reimported["history"]), 1)

    def test_html_explicit_opposite_title_or_h1_is_rejected_despite_user_links(self):
        for kind, markup in (
            ("following", "<title>Followers</title><h1>Followers</h1>"),
            ("following", "<title>Takipçiler</title>"),
            ("following", "<title>TAKİPÇİLER</title>"),
            ("following", "<h1>Takipçi</h1>"),
            ("followers", "<title>Following</title><h1>Following</h1>"),
            ("followers", "<h1>Takip edilenler</h1>"),
        ):
            with self.subTest(kind=kind, markup=markup):
                first = self.write_json("followers_1.json", [follower("bob")])
                chosen = self.write_json("following.json", {"relationships_following": [following("carol")]})
                html = '<html>' + markup + '<a href="https://www.instagram.com/alice/">alice</a></html>'
                if kind == "following":
                    chosen = self.write_html("following.html", html)
                else:
                    first = self.write_html("followers_1.html", html)
                with self.assertRaises(exports.ExportImportError):
                    self.load([first, chosen])


if __name__ == "__main__":
    unittest.main()
