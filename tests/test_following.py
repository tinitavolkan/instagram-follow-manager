"""Offline profile-control regressions with a real, isolated Playwright DOM.

All URLs are fulfilled locally; no Instagram profile or credentials are opened.
Production following.py is loaded with inert config/browser dependencies.
"""

import importlib.util
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


def load_following():
    package_name = "_offline_following_fixture"
    package = ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    browser = ModuleType(package_name + ".browser")
    browser.page = None
    browser.run_bot = Mock()
    browser.is_rate_limited = Mock(return_value=False)
    browser.cooldown_wait = Mock()
    browser.human_like_click = Mock()
    config = ModuleType(package_name + ".config")
    config._scaled = lambda *args, **kwargs: 0
    paths = ModuleType(package_name + ".paths")
    paths.IG_URL = "https://www.instagram.com"
    timing = ModuleType(package_name + ".timing")
    timing.stoppable_wait = lambda stop, *args, **kwargs: bool(stop and stop.is_set())
    for name, module in (("browser", browser), ("config", config), ("paths", paths), ("timing", timing)):
        sys.modules[f"{package_name}.{name}"] = module
        setattr(package, name, module)
    path = Path(__file__).resolve().parents[1] / "instagram_bot" / "following.py"
    spec = importlib.util.spec_from_file_location(package_name + ".following", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FollowingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.following = load_following()
        cls.playwright = sync_playwright().start()
        # Installed Chrome uses a new isolated profile, never the user's session.
        cls.chromium = cls.playwright.chromium.launch(headless=True, channel="chrome")

    @classmethod
    def tearDownClass(cls):
        cls.chromium.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.chromium.new_context()
        self.page = self.context.new_page()
        self.fixture = ""
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=self.fixture))
        self.browser = self.following.browser
        self.browser.page = self.page
        self.browser.run_bot.reset_mock()
        self.browser.is_rate_limited.reset_mock(return_value=True, side_effect=True)
        self.browser.is_rate_limited.return_value = False
        self.browser.cooldown_wait.reset_mock()
        self.browser.human_like_click = Mock(side_effect=self.click)

    def tearDown(self):
        self.context.close()

    @staticmethod
    def click(page, locator):
        locator.click()
        return True

    def profile(self, content, outside=""):
        self.fixture = f"<main><header><h2>alice</h2>{content}</header>{outside}</main>"

    def test_following_counter_is_not_a_relationship(self):
        self.profile("<a>100 following</a><button onclick=\"this.textContent='Following'\">Follow</button>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.status, "followed")
        self.assertTrue(result.action_submitted)
        self.browser.human_like_click.assert_called_once()

    def test_unrelated_recommendation_follow_button_is_not_clicked(self):
        self.profile("<a>100 following</a>", "<article><h2>bob</h2><button id='other'>Follow</button></article>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.status, "retryable_error")
        self.assertEqual(result.reason, "profile_control_missing")
        self.browser.human_like_click.assert_not_called()

    def test_unrelated_following_button_does_not_confirm_target(self):
        self.profile("<button>Follow</button>", "<article><h2>bob</h2><button>Following</button></article>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.status, "unconfirmed")
        self.assertTrue(result.action_submitted)

    def test_following_count_in_button_is_not_exact_match(self):
        self.profile("<button>100 following</button>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.status, "retryable_error")
        self.browser.human_like_click.assert_not_called()

    def test_existing_exact_english_and_turkish_labels(self):
        for label, expected in (("Following", "already_following"), ("Takiptesin", "already_following"),
                                ("Requested", "requested_existing"), ("İstek Gönderildi", "requested_existing"),
                                ("İSTEK GÖNDERİLDİ", "requested_existing")):
            with self.subTest(label=label):
                self.profile(f"<button>{label}</button>")
                result = self.following.follow_user("alice", cb=Mock())
                self.assertEqual(result.status, expected)
                self.assertFalse(result.action_submitted)
        self.browser.human_like_click.assert_not_called()

    def test_follow_changes_to_turkish_request(self):
        self.profile("<button onclick=\"this.textContent='İstek Gönderildi'\">Takip Et</button>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.status, "requested")
        self.assertTrue(result.action_submitted)

    def test_multiple_profile_controls_are_ambiguous(self):
        self.profile("<button>Follow</button><button>Following</button>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.reason, "profile_control_ambiguous")
        self.browser.human_like_click.assert_not_called()

    def test_follow_back_turkish_nested_button_is_clicked_and_verified(self):
        for label, final_label, expected in (
            ("Sen de Onu Takip Et", "Takiptesin", "followed"),
            ("SEN DE ONU TAKİP ET", "İstek Gönderildi", "requested"),
            ("  Sen de\n Onu  Takip Et  ", "Takiptesin", "followed"),
        ):
            with self.subTest(label=label):
                self.browser.human_like_click.reset_mock()
                self.profile(
                    '<div class="html-div"><button class="_aswp _aswr _aswu _aswy _asw_ _asx2" '
                    f'type="button" onclick="this.textContent=\'{final_label}\'">'
                    '<div class="html-div" style="--x-height: 100%;">'
                    f'<div class="_ap3a _aaco _aacw _aad6 _aade" dir="auto">{label}</div>'
                    '</div></button></div>'
                )
                result = self.following.follow_user("alice", cb=Mock())
                self.assertEqual(result.status, expected)
                self.assertTrue(result.action_submitted)
                self.browser.human_like_click.assert_called_once()

    def test_turkish_follow_back_outside_target_profile_is_not_clicked(self):
        self.profile("<a>100 following</a>",
                     "<article><h2>bob</h2><button>Sen de Onu Takip Et</button></article>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.reason, "profile_control_missing")
        self.browser.human_like_click.assert_not_called()

    def test_turkish_follow_back_still_requires_exact_label(self):
        self.profile("<button>Sen de Onu Takip Et önerisi</button>")
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.reason, "profile_control_missing")
        self.browser.human_like_click.assert_not_called()

    def test_wrong_profile_heading_is_not_used(self):
        self.fixture = "<main><header><h2>bob</h2><button>Following</button></header></main>"
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual(result.reason, "profile_identity_unconfirmed")
        self.assertEqual(result.status, "retryable_error")

    def test_navigation_timeout_is_retryable_without_click(self):
        self.browser.page = SimpleNamespace(goto=Mock(side_effect=PWTimeout("slow profile")))
        result = self.following.follow_user("alice", cb=Mock())
        self.assertEqual((result.status, result.reason), ("retryable_error", "profile_timeout"))
        self.assertFalse(result.action_submitted)
        self.browser.human_like_click.assert_not_called()

    def test_stopped_task_does_not_open_session_or_submit(self):
        stop = Event()
        stop.set()
        self.browser.page = None
        result = self.following.follow_user("alice", cb=Mock(), stop_event=stop)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.action_submitted)
        self.browser.run_bot.assert_not_called()
        self.browser.human_like_click.assert_not_called()

    def test_cancellation_after_click_preserves_action_flag(self):
        self.profile("<button>Follow</button>")
        stop = Event()

        def click_then_stop(page, locator):
            locator.click()
            stop.set()
            return True

        self.browser.human_like_click.side_effect = click_then_stop
        result = self.following.follow_user("alice", cb=Mock(), stop_event=stop)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.action_submitted)

    def test_invalid_username_is_the_only_local_exclusion(self):
        for username in ("", "invalid/name", "invalid name", "x" * 31):
            with self.subTest(username=username):
                result = self.following.follow_user(username, cb=Mock())
                self.assertEqual((result.status, result.reason), ("excluded", "invalid_username"))
        self.browser.human_like_click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
