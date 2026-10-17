"""Single-user relationship actions against routed, entirely local browser DOM."""
import importlib
import json
from threading import Event
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright, Locator

try:
    unfollowing = importlib.import_module("instagram_bot.unfollowing")
    sessions = importlib.import_module("instagram_bot.instagram_session")
except ModuleNotFoundError:
    unfollowing = sessions = None


def navigation(account="owner"):
    return (f'<nav style="width:72px"><a href="/">Home</a><a href="/reels/">Reels</a>'
            f'<a id="own-profile" href="/{account}/" aria-label="Profile">Profile</a></nav>')


def fixture(account="owner", relationship="Following", heading="target", *,
            popup="dialog", duplicate=False, switch_before_submit=False,
            update_control=True, delay=0, extra_header="", decoy=False,
            hide_background=False, nested_menu=False):
    # Only the fixture's final submit endpoint changes the simulated server.
    state = json.dumps(relationship)
    label = "Takibi bırak" if relationship == "Takiptesin" else "Unfollow"
    action = ("" if not update_control else
              f"setTimeout(()=>document.getElementById('relationship').textContent='Follow',{delay});")
    switch = ("document.getElementById('own-profile').href='/different/';" if switch_before_submit else "")
    duplicate_button = f'<button onclick="submitUnfollow()">{label}</button>' if duplicate else ""
    popup_html = (f'<div role="{popup}"><button onclick="submitUnfollow()">{label}</button>'
                  f'{duplicate_button}<button>Cancel</button></div>')
    if nested_menu:
        popup_html = f'<div role="dialog">{popup_html}</div>'
    if popup == "loose":
        popup_html = f'<div><button onclick="submitUnfollow()">{label}</button></div>'
    unrelated = '<button onclick="record(\'decoy\')">Unfollow</button>' if decoy else ""
    hide = ("document.querySelector('nav').setAttribute('aria-hidden','true');"
            "document.querySelector('main').setAttribute('aria-hidden','true');" if hide_background else "")
    return f'''<!doctype html><html><body>{navigation(account)}
      <main><header><h2>{heading}</h2><button id="relationship" onclick="openPopup()">{relationship}</button>
        {extra_header}</header></main>{unrelated}<div id="popup"></div>
      <script>
        window.actions=JSON.parse(sessionStorage.getItem('actions')||'[]');
        function record(value){{actions.push(value);sessionStorage.setItem('actions',JSON.stringify(actions));}}
        function openPopup(){{
          record('open'); if(['Following','Takiptesin'].includes({state})){{
            document.getElementById('popup').innerHTML={json.dumps(popup_html)};{hide}{switch}
          }}
        }}
        async function submitUnfollow(){{
          record('submit'); await fetch('/offline/unfollow',{{method:'POST'}});
          document.getElementById('popup').innerHTML='';
          document.querySelector('nav').removeAttribute('aria-hidden');
          document.querySelector('main').removeAttribute('aria-hidden');{action}
        }}
      </script></body></html>'''


class FastPage:
    def __init__(self, page, on_wait=None):
        self.page, self.now, self.on_wait = page, 0.0, on_wait
        self.listeners = []

    def __getattr__(self, name):
        return getattr(self.page, name)

    def wait_for_timeout(self, milliseconds):
        self.now += milliseconds / 1000
        if self.on_wait:
            self.on_wait(self)
        self.page.wait_for_timeout(1)

    def on(self, name, callback):
        self.listeners.append((name, callback))
        self.page.on(name, callback)

    def remove_listener(self, name, callback):
        self.listeners.remove((name, callback))
        self.page.remove_listener(name, callback)


class UnfollowingDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, channel="chrome")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.initial = fixture()
        self.refresh = None
        self.target_body = None
        self.requests = []
        self.target_visits = 0
        self.submissions = 0
        self.status = 200
        self.submit_status = 200
        self.context.route("**/*", self.route)

    def tearDown(self):
        self.context.close()

    def route(self, route):
        self.requests.append(route.request.url)
        if route.request.url.endswith("/offline/unfollow"):
            self.submissions += 1
            route.fulfill(status=self.submit_status, content_type="application/json", body='{}')
            return
        is_target = route.request.url.endswith("/target/")
        if is_target:
            self.target_visits += 1
        body = self.initial
        if is_target and self.target_body is not None:
            body = self.target_body
        if is_target and self.target_visits > 1:
            body = self.refresh or fixture(relationship="Follow")
        route.fulfill(status=self.status if is_target else 200, content_type="text/html; charset=utf-8", body=body)

    def perform(self, target="target", expected="owner", stop=None, on_wait=None):
        self.assertIsNotNone(unfollowing, "The single-user unfollow operation is missing")
        self.target_visits = 0
        self.submissions = 0
        self.page.goto("https://www.instagram.com/")
        self.driver = FastPage(self.page, on_wait)
        with patch.object(unfollowing.time, "monotonic", lambda: self.driver.now):
            result = unfollowing.unfollow_user(self.driver, target, expected, stop)
        self.assertEqual(self.driver.listeners, [], "Temporary response listeners must be detached")
        return result

    def test_confirms_one_unfollow_in_target_dialog_and_on_fresh_target_page(self):
        self.initial = fixture(decoy=True)
        result = self.perform()
        self.assertEqual(result.status, "unfollowed", result.detail)
        self.assertTrue(result.action_submitted)
        self.assertEqual(self.submissions, 1)
        self.assertEqual(self.target_visits, 2)
        self.assertEqual(self.page.evaluate("actions"), ["open", "submit"])

    def test_verification_survives_header_buttons_disappearing_between_reads(self):
        self.initial = fixture(extra_header=''.join('<button class="decoration">Options</button>' for _ in range(5)))
        original = Locator.get_attribute
        removed = []

        def disappearing_buttons(locator, name, *args, **kwargs):
            if self.submissions and not removed:
                removed.append(True)
                self.page.evaluate("() => document.querySelectorAll('header .decoration').forEach(el => el.remove())")
            return original(locator, name, *args, **kwargs)

        with patch.object(Locator, 'get_attribute', new=disappearing_buttons):
            result = self.perform()
        self.assertEqual(result.status, 'unfollowed', result.detail)
        self.assertEqual(self.submissions, 1)
        self.assertEqual(self.target_visits, 2)

    def test_turkish_relationship_and_scoped_menu_are_supported(self):
        self.initial = fixture(relationship="Takiptesin", popup="menu")
        result = self.perform()
        self.assertEqual(result.status, "unfollowed", result.detail)
        self.assertEqual(self.submissions, 1)

    def test_turkish_following_button_with_svg_arrow_title_is_clickable(self):
        self.initial = fixture(relationship='Takiptesin').replace(
            '>Takiptesin</button>',
            '><div><div>Takiptesin</div><svg role="img" aria-label="Aşağı Ok Simgesi">'
            '<title>Aşağı Ok Simgesi</title></svg></div></button>')
        result = self.perform()
        self.assertEqual(result.status, 'unfollowed', result.detail)
        self.assertEqual(self.submissions, 1)
        self.assertEqual(self.target_visits, 2)

    def test_modal_accessibility_hidden_background_retains_visible_account_proof(self):
        self.initial = fixture(hide_background=True)
        result = self.perform()
        self.assertEqual(result.status, "unfollowed", result.detail)
        self.assertEqual(self.submissions, 1)

    def test_account_changed_in_accessibility_hidden_background_is_rejected(self):
        self.initial = fixture(hide_background=True, switch_before_submit=True)
        result = self.perform()
        self.assertEqual(result.status, "error")
        self.assertEqual(self.submissions, 0)

    def test_modal_does_not_accept_ambiguous_accessibility_hidden_account_links(self):
        self.initial = fixture(hide_background=True) + (
            '<nav aria-hidden="true"><a href="/different/" aria-label="Profile">Profile</a></nav>')
        result = self.perform()
        self.assertEqual(result.status, "error")
        self.assertEqual(self.submissions, 0)

    def test_css_hidden_other_account_template_does_not_replace_visible_owner(self):
        self.initial = fixture(hide_background=True) + (
            '<nav style="display:none"><a href="/different/" aria-label="Profile">Profile</a></nav>')
        result = self.perform()
        self.assertEqual(result.status, "unfollowed", result.detail)
        self.assertEqual(self.submissions, 1)

    def test_nested_dialog_and_menu_are_one_confirmation_scope(self):
        self.initial = fixture(popup="menu", nested_menu=True)
        result = self.perform()
        self.assertEqual(result.status, "unfollowed", result.detail)
        self.assertEqual(self.submissions, 1)

    def test_story_highlights_menu_is_not_a_second_confirmation_popup(self):
        # Live Instagram gives the always-visible story highlights role=menu.
        self.initial = fixture() + (
            '<div role="menu"><ul><li><a href="/stories/highlights/123/">'
            '<button>GYM</button></a></li></ul></div>')
        result = self.perform()
        self.assertEqual(result.status, "unfollowed", result.detail)
        self.assertEqual(self.submissions, 1)
        self.assertEqual(self.target_visits, 2)

    def test_unavailable_target_is_reported_without_owner_or_button_timeout(self):
        self.target_body = '<h2>Üzgünüz, bu sayfaya ulaşılamıyor.</h2><p>Sayfa kaldırılmış olabilir.</p>'
        result = self.perform()
        self.assertEqual(result.status, 'unavailable')
        self.assertIn('profile ulaşılamıyor', result.detail)
        self.assertFalse(result.action_submitted)
        self.assertEqual(self.submissions, 0)
        self.assertEqual(self.target_visits, 1)

    def test_live_instagram_span_error_layout_is_recognized(self):
        self.target_body = navigation() + '''<main role="main"><div><span>
            Üzgünüz, bu sayfaya ulaşılamıyor.</span><div>
            Tıkladığın bağlantı bozuk olabilir veya sayfa kaldırılmış olabilir.
            <a href="/">Instagram'a geri dön.</a></div></div></main>'''
        result = self.perform()
        self.assertEqual(result.status, 'unavailable')
        self.assertIn('profile ulaşılamıyor', result.detail)
        self.assertEqual(self.submissions, 0)

    def test_next_target_is_navigated_after_previous_unavailable_page(self):
        self.target_body = navigation() + '''<main><div><span>Üzgünüz, bu sayfaya ulaşılamıyor.</span>
            <p>Tıkladığın bağlantı bozuk olabilir veya sayfa kaldırılmış olabilir.
            <a href="/">Instagram'a geri dön.</a></p></div></main>'''
        first = self.perform()
        self.assertEqual(first.status, 'unavailable', first.detail)
        visits = []

        def next_profile(route):
            visits.append(route.request.url)
            route.fulfill(status=200, content_type='text/html; charset=utf-8',
                          body=fixture(heading='next', relationship='Follow' if self.submissions else 'Following'))

        self.context.route('https://www.instagram.com/next/', next_profile)
        with patch.object(unfollowing.time, 'monotonic', lambda: self.driver.now):
            second = unfollowing.unfollow_user(self.driver, 'next', 'owner')
        self.assertEqual(second.status, 'unfollowed', second.detail)
        self.assertEqual(self.submissions, 1)
        self.assertEqual(len(visits), 2, 'Next target must load and then reload for verification')
        self.assertEqual(self.page.url, 'https://www.instagram.com/next/')

    def test_hidden_span_error_template_is_ignored(self):
        self.initial = fixture() + '''<main style="display:none"><div>
            <span>Üzgünüz, bu sayfaya ulaşılamıyor.</span>
            <p>Tıkladığın bağlantı bozuk olabilir veya sayfa kaldırılmış olabilir.</p>
            <a href="/">Instagram'a geri dön.</a></div></main>'''
        result = self.perform()
        self.assertEqual(result.status, 'unfollowed', result.detail)

    def test_unavailable_refresh_never_counts_as_success(self):
        self.refresh = "<h2>Sorry, this page isn't available.</h2>"
        result = self.perform()
        self.assertEqual(result.status, 'unavailable')
        self.assertIn('profile ulaşılamıyor', result.detail)
        self.assertTrue(result.action_submitted)
        self.assertEqual(self.submissions, 1)

    def test_unavailable_words_in_ordinary_profile_text_are_not_an_error_page(self):
        self.initial = fixture() + '<p>Üzgünüz, bu sayfaya ulaşılamıyor.</p>'
        result = self.perform()
        self.assertEqual(result.status, 'unfollowed', result.detail)

    def test_rate_response_takes_precedence_over_unavailable_heading(self):
        self.target_body = '<h2>Üzgünüz, bu sayfaya ulaşılamıyor.</h2>'
        self.status = 429
        result = self.perform()
        self.assertEqual(result.status, 'restricted')
        self.assertEqual(self.submissions, 0)

    def test_two_independent_unfollow_popups_are_still_rejected(self):
        self.initial = fixture() + '<div role="menu"><button>Unfollow</button></div>'
        result = self.perform()
        self.assertEqual(result.status, "error", result.detail)
        self.assertFalse(result.action_submitted)
        self.assertEqual(self.submissions, 0)

    def test_already_not_following_does_not_click_or_submit(self):
        self.initial = fixture(relationship="Takip et")
        result = self.perform()
        self.assertEqual(result.status, "already_not_following", result.detail)
        self.assertFalse(result.action_submitted)
        self.assertEqual(self.submissions, 0)
        self.assertEqual(self.page.evaluate("actions"), [])

    def test_requested_relationship_is_not_cancelled(self):
        self.initial = fixture(relationship="Requested")
        result = self.perform()
        self.assertEqual(result.status, "error")
        self.assertEqual(self.page.evaluate("actions"), [])
        self.assertEqual(self.submissions, 0)

    def test_invalid_names_and_self_action_are_rejected_without_navigation(self):
        for target, expected in (("bad/name", "owner"), ("target", ""),
                                 ("https://instagram.com/target", "owner"), ("owner", "owner"),
                                 ("..", "owner")):
            with self.subTest(target=target, expected=expected):
                self.requests.clear()
                result = self.perform(target, expected)
                self.assertEqual(result.status, "error")
                self.assertFalse(result.action_submitted)
                self.assertEqual(self.requests, ["https://www.instagram.com/"])

    def test_wrong_session_account_never_opens_target(self):
        self.initial = fixture(account="other")
        result = self.perform()
        self.assertEqual(result.status, "error")
        self.assertEqual(self.target_visits, 0)
        self.assertEqual(self.submissions, 0)

    def test_account_change_after_opening_confirmation_prevents_submit(self):
        self.initial = fixture(switch_before_submit=True)
        result = self.perform()
        self.assertEqual(result.status, "error", result.detail)
        self.assertFalse(result.action_submitted)
        self.assertEqual(self.submissions, 0)

    def test_target_heading_or_multiple_relationship_controls_are_not_actionable(self):
        for initial in (fixture(heading="other"),
                        fixture(extra_header='<button>Following</button>')):
            with self.subTest(initial=initial):
                self.initial = initial
                result = self.perform()
                self.assertEqual(result.status, "error")
                self.assertEqual(self.submissions, 0)
                self.assertEqual(self.page.evaluate("actions"), [])

    def test_unscoped_or_ambiguous_confirmation_never_submits(self):
        for initial in (fixture(popup="loose"), fixture(duplicate=True)):
            with self.subTest(initial=initial):
                self.initial = initial
                result = self.perform()
                self.assertEqual(result.status, "error")
                self.assertFalse(result.action_submitted)
                self.assertEqual(self.submissions, 0)

    def test_missing_post_submit_confirmation_keeps_unconfirmed(self):
        self.initial = fixture(update_control=False)
        result = self.perform()
        self.assertEqual(result.status, "unconfirmed")
        self.assertTrue(result.action_submitted)
        self.assertEqual(self.submissions, 1)

    def test_recommendation_follow_buttons_are_not_target_success_evidence(self):
        self.initial = fixture(update_control=False) + '<aside><button>Follow</button><button>Takip Et</button></aside>'
        result = self.perform()
        self.assertEqual(result.status, 'unconfirmed')
        self.assertEqual(self.submissions, 1)

    def test_optimistic_follow_label_is_not_success_when_fresh_page_still_follows(self):
        self.refresh = fixture(relationship="Following")
        result = self.perform()
        self.assertEqual(result.status, "unconfirmed")
        self.assertTrue(result.action_submitted)
        self.assertEqual(self.submissions, 1)
        self.assertEqual(self.target_visits, 2)

    def test_fresh_page_account_change_never_reports_confirmed_unfollow(self):
        self.refresh = fixture(account="different", relationship="Follow")
        result = self.perform()
        self.assertEqual(result.status, "unconfirmed")
        self.assertTrue(result.action_submitted)
        self.assertEqual(self.submissions, 1)

    def test_delayed_verified_ui_state_is_waited_for_without_resubmitting(self):
        self.initial = fixture(delay=50)
        result = self.perform()
        self.assertEqual(result.status, "unfollowed", result.detail)
        self.assertEqual(self.submissions, 1)

    def test_stop_before_start_does_not_open_target(self):
        stop = Event()
        stop.set()
        result = self.perform(stop=stop)
        self.assertEqual(result.status, "stopped")
        self.assertFalse(result.action_submitted)
        self.assertEqual(self.target_visits, 0)

    def test_stop_after_submit_never_reports_success(self):
        stop = Event()

        def stop_after_submit(driver):
            if self.submissions:
                stop.set()

        self.initial = fixture(update_control=False)
        result = self.perform(stop=stop, on_wait=stop_after_submit)
        self.assertEqual(result.status, "stopped")
        self.assertTrue(result.action_submitted)
        self.assertEqual(self.submissions, 1)

    def test_instagram_auth_rate_responses_prevent_actions(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.status = status
                result = self.perform()
                self.assertEqual(result.status, "restricted", result.detail)
                self.assertEqual(self.submissions, 0)

    def test_rate_response_after_submit_is_restricted_with_submitted_flag(self):
        self.submit_status = 429
        result = self.perform()
        self.assertEqual(result.status, "restricted", result.detail)
        self.assertTrue(result.action_submitted)
        self.assertEqual(self.submissions, 1)

    def test_visible_restriction_stops_before_any_navigation(self):
        self.initial += '<div role="alert">Daha sonra tekrar dene</div>'
        result = self.perform()
        self.assertEqual(result.status, "restricted")
        self.assertEqual(self.target_visits, 0)

    def test_session_helper_accepts_compact_own_avatar_but_rejects_suggestions(self):
        self.assertIsNotNone(sessions, "The credential-free session module is missing")
        self.initial = fixture().replace('aria-label="Profile">Profile',
            '><img width="24" height="24" alt="owner\'in profil resmi"')
        self.page.goto("https://www.instagram.com/")
        self.assertEqual(sessions.session_username(self.page), "owner")
        self.page.set_content('<aside><a href="/other/"><img width="24" height="24" alt="other\'s profile picture"></a></aside>')
        self.assertIsNone(sessions.session_username(self.page))


if __name__ == "__main__":
    unittest.main()
