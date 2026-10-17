"""Read-only comparison regressions using an isolated, entirely local DOM."""

import importlib
from html import escape
import json
from threading import Event
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


try:
    non_followers = importlib.import_module("instagram_bot.non_followers")
except ModuleNotFoundError:
    non_followers = None


class NonFollowersResultTests(unittest.TestCase):
    def test_case_insensitive_difference_is_available_only_for_completed_scans(self):
        # A partial read must never label an unseen follower a non-follower.
        self.assertIsNotNone(non_followers, "The read-only comparison is not implemented")
        for status in ("completed", "incomplete", "restricted", "stopped", "error"):
            with self.subTest(status=status):
                result = non_followers.NonFollowersResult(
                    status, followers=("ALICE",), following=("alice", "Bob", "bob", "charlie")
                )
                self.assertEqual(result.not_following_back,
                                 ("bob", "charlie") if status == "completed" else ())


def row(username, *, href=None):
    address = href if href is not None else f"/{username}/"
    return (f'<div role="listitem" style="height:55px"><a href="{address}"></a>'
            f'<a href="{address}">{username}</a><button onclick="actions.push(\'relationship\')">Following</button></div>')


def profile_fixture(followers, following, *, follower_count=None, following_count=None,
                    sidebar='<aside><a href="/owner/"><svg aria-label="Profile"></svg>Profile</a></aside>',
                    follower_extra="", following_extra="", count_change="", header_extra="", escape_close=True):
    fc = str(len(followers)) if follower_count is None else follower_count
    gc = str(len(following)) if following_count is None else following_count
    escape_handler = '''document.addEventListener('keydown', e=>{if(e.key==='Escape'){
        document.querySelectorAll('[role=dialog]').forEach(d=>{if(d.style.display!=='none')closeList(d)});
      }});''' if escape_close else ""
    return f'''<!doctype html><html><body style="padding-left:96px">{sidebar}
    <main><header><h2>owner</h2>
    <a id="followers-link" href="/owner/followers/" onclick="event.preventDefault();openList('followers')">{fc} followers</a>
    <a id="following-link" href="/owner/following/" onclick="event.preventDefault();openList('following')">{gc} following</a>
    {header_extra}</header><a href="/stranger/">Profile</a></main>
    <div id="followers" role="dialog" style="display:none"><h2>Followers</h2>
      <button aria-label="Close" onclick="closeList(this.parentElement)">Close</button>
      <div class="users" style="height:120px;overflow-y:auto">{''.join(row(u) for u in followers)}{follower_extra}</div>
    </div>
    <div id="following" role="dialog" style="display:none"><h2>Following</h2>
      <button aria-label="Close" onclick="closeList(this.parentElement)">Close</button>
      <div class="users" style="height:120px;overflow-y:auto">{''.join(row(u) for u in following)}{following_extra}</div>
    </div>
    <script>window.actions=JSON.parse(sessionStorage.getItem('actions')||'[]');
      actions.push=function(...items) {{ const size=Array.prototype.push.apply(this,items);
        sessionStorage.setItem('actions',JSON.stringify(this));return size; }};
      function openList(name) {{ actions.push(name);document.getElementById(name).style.display='block'; }}
      function closeList(dialog) {{ dialog.style.display='none'; {count_change} }}
      {escape_handler}
    </script></body></html>'''


def profile_picture_link(username="owner", alt="owner's profile picture"):
    """A sized local avatar supplies real visible pixels without an image request."""
    return (f'<a href="/{username}/"><img width="32" height="32" '
            f'alt="{escape(alt, quote=True)}" '
            'src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' '
            'width=\'32\' height=\'32\'%3E%3Crect width=\'32\' height=\'32\' '
            'fill=\'gray\'/%3E%3C/svg%3E"></a>')


def compact_sidebar(profile_link, *, container="nav", other_route="/reels/", extra=""):
    # Home and a separate SVG navigation control distinguish the sidebar from
    # a story or suggestion card; the avatar itself contains no visible label.
    opening = ('<div role="navigation"' if container == "navigation" else f'<{container}')
    closing = "div" if container == "navigation" else container
    return (opening + ' style="position:fixed;left:0;top:0;width:72px;height:700px">'
            '<a href="/"><svg width="24" height="24" aria-label="Home"></svg></a>'
            f'<a href="{other_route}"><svg width="24" height="24" aria-label="Reels"></svg></a>'
            + profile_link + extra + f'</{closing}>')


def hash_profile_stats(fixture, *, follower_label='<span title="1">1</span> takipçi', following_label='1 takip'):
    """Model the observed profile header's '#' anchors rather than URL routes."""
    return (fixture.replace('href="/owner/followers/"', 'href="#" role="link"')
            .replace('href="/owner/following/"', 'href="#" role="link"')
            .replace('1 followers</a>', follower_label + '</a>')
            .replace('1 following</a>', following_label + '</a>'))


def loaded_batch_fixture(row_height=55):
    """Append two users only after traversing a 40-row rendered list batch."""
    initial = [f"user{index:02}" for index in range(40)]
    fixture = profile_fixture(initial, initial, follower_count="42", following_count="42")
    fixture = fixture.replace('height:120px;overflow-y:auto', 'height:90px;overflow-y:auto')
    final_rows = json.dumps({
        "followers": row("back01") + row("back02"),
        "following": row("back01") + row("outsider"),
    })
    fixture = fixture.replace("</script>", f'''
      const finalRows={final_rows};
      window.scrollStats=JSON.parse(sessionStorage.getItem('scrollStats')||
        '{{"followers":{{"events":0,"appended":false,"times":[]}},"following":{{"events":0,"appended":false,"times":[]}}}}');
      for(const kind of ['followers','following']) {{
        const users=document.querySelector('#'+kind+' .users');
        users.addEventListener('scroll',()=>{{
          scrollStats[kind].events++;
          scrollStats[kind].times.push(window.virtualScanTime||0);
          if(!users.dataset.appended && users.scrollTop+users.clientHeight>=users.scrollHeight-1) {{
            users.dataset.appended='true';
            users.insertAdjacentHTML('beforeend',finalRows[kind]);
            scrollStats[kind].appended=true;
          }}
          sessionStorage.setItem('scrollStats',JSON.stringify(scrollStats));
        }});
      }}</script>''')
    return fixture.replace('height:55px', f'height:{row_height}px')


class FastPage:
    """Keep the real offline DOM, while making the scan's pacing clock virtual."""

    def __init__(self, page, on_wait=None):
        self.page = page
        self.now = 0.0
        self.listeners = []
        self.on_wait = on_wait

    def __getattr__(self, name):
        return getattr(self.page, name)

    def wait_for_timeout(self, milliseconds):
        self.now += milliseconds / 1000
        if self.on_wait:
            self.on_wait(self)
        self.page.wait_for_timeout(0)

    def on(self, name, callback):
        self.listeners.append((name, callback))
        self.page.on(name, callback)

    def remove_listener(self, name, callback):
        self.listeners.remove((name, callback))
        self.page.remove_listener(name, callback)


class NonFollowersDOMTests(unittest.TestCase):
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
        self.fixture = profile_fixture(["Alice", "mixed"], ["alice", "BOB"])
        # This catches every request, including any accidental per-user visit.
        self.requests = []
        self.response_status = 200
        self.owner_visits = 0
        self.refresh_fixture = None
        self.refresh_status = None
        self.context.route("**/*", self.route)

    def route(self, route):
        self.requests.append(route.request.url)
        own_profile = route.request.url.endswith("/owner/")
        if own_profile:
            self.owner_visits += 1
        final_visit = own_profile and self.owner_visits == 2
        status = (self.refresh_status if final_visit and self.refresh_status is not None
                  else self.response_status if own_profile else 200)
        fixture = self.refresh_fixture if final_visit and self.refresh_fixture is not None else self.fixture
        route.fulfill(status=status, content_type="text/html; charset=utf-8", body=fixture)

    def tearDown(self):
        self.context.close()

    def scan(self, stop=None, *, fast=True, on_wait=None):
        self.assertIsNotNone(non_followers, "The read-only comparison is not implemented")
        self.owner_visits = 0
        self.page.goto("https://www.instagram.com/")
        self.page.evaluate("sessionStorage.clear(); window.actions=[];")
        self.driver = FastPage(self.page, on_wait=on_wait)
        if fast:
            with patch.object(non_followers.time, "monotonic", lambda: self.driver.now):
                return non_followers.scan_non_followers(self.driver, stop or Event())
        return non_followers.scan_non_followers(self.driver, stop or Event())

    def test_reads_own_two_lists_and_never_visits_or_changes_user_profiles(self):
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.username, "owner")
        self.assertEqual(result.followers, ("alice", "mixed"))
        self.assertEqual(result.following, ("alice", "bob"))
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertEqual((result.expected_followers, result.expected_following), (2, 2))
        self.assertTrue(result.checked_at)
        self.assertEqual(self.page.evaluate("actions"), ["followers", "following"])
        self.assertEqual(self.requests, ["https://www.instagram.com/", "https://www.instagram.com/owner/",
                                        "https://www.instagram.com/owner/"])

    def test_exact_title_is_used_when_the_displayed_count_is_abbreviated(self):
        self.fixture = profile_fixture(["alice"], ["bob"], follower_count="1.2K")
        self.fixture = self.fixture.replace('id="followers-link"', 'id="followers-link" title="1,234"')
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.expected_followers, 1234)
        self.assertEqual(result.not_following_back, ())

    def test_unabbreviated_english_and_turkish_counts_accept_thousands_grouping(self):
        for text in ("1,234 followers", "1.234 takipçi", "1 234 takipçi", "1234 followers"):
            with self.subTest(text=text):
                self.fixture = profile_fixture(["alice"], ["bob"], follower_count="1234")
                self.fixture = self.fixture.replace("1234 followers</a>", text + "</a>")
                result = self.scan()
                self.assertEqual(result.expected_followers, 1234)
                self.assertEqual(result.status, "incomplete")

    def test_abbreviated_or_ambiguous_counts_do_not_start_a_list_scan(self):
        for text in ("1.2K", "1,2 B", "1.23", "1,234.567", "about 12", "12M"):
            with self.subTest(text=text):
                self.fixture = profile_fixture(["alice"], ["bob"], follower_count=text)
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.not_following_back, ())
                self.assertEqual(self.page.evaluate("actions"), [])

    def test_sidebar_profile_identity_is_required_even_with_other_profile_links(self):
        self.fixture = profile_fixture(["alice"], ["bob"], sidebar="")
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.username, "")
        self.assertEqual(self.requests, ["https://www.instagram.com/"])

    def test_turkish_sidebar_svg_identifies_the_signed_in_profile(self):
        self.fixture = profile_fixture(["alice"], ["bob"],
            sidebar='<nav><a href="/owner/"><svg aria-label="Profil"></svg></a></nav>')
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.username, "owner")

    def test_reserved_external_and_malformed_routes_do_not_become_users(self):
        invalid = ["/explore/", "/accounts/", "https://example.com/evil/", "/bob/followers/",
                   "/bad%20name/", "/bad-name/", "/bob/?x=1", "/bob/#x", "javascript:alert(1)"]
        self.fixture = profile_fixture(["alice"], ["bob"],
            follower_extra="".join(row("ignored", href=href) for href in invalid))
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.not_following_back, ("bob",))

    def test_suggested_users_do_not_fill_missing_followers(self):
        self.fixture = profile_fixture(["alice", "mixed"], ["bob"], follower_count="3",
            follower_extra='<h3>Suggested for you</h3>' + row("bob"))
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.followers, ("alice", "mixed"))
        self.assertEqual(result.not_following_back, ())

    def test_profile_count_change_after_reading_suppresses_comparison(self):
        self.fixture = profile_fixture(["alice"], ["bob"],
            count_change="document.getElementById('followers-link').textContent='2 followers';")
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.not_following_back, ())

    def test_unlabelled_div_user_rows_are_collected_from_their_relationship_button(self):
        self.fixture = profile_fixture(["alice"], ["bob"]).replace(' role="listitem"', '')
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.not_following_back, ("bob",))

    def test_title_on_nested_count_span_is_authoritative(self):
        self.fixture = profile_fixture(["alice"], ["bob"])
        self.fixture = self.fixture.replace('1 followers</a>', '<span title="1">1K</span> takipçi</a>')
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.expected_followers, 1)

    def test_spa_profile_and_dialog_rendering_are_waited_for_without_new_requests(self):
        self.fixture = profile_fixture(["alice"], ["bob"])
        self.fixture = self.fixture.replace("document.getElementById(name).style.display='block';",
            "setTimeout(()=>document.getElementById(name).style.display='block', 150);")
        self.fixture = self.fixture.replace("</script>",
            "document.querySelector('main header').style.display='none';"
            "setTimeout(()=>document.querySelector('main header').style.display='block',250);</script>")
        result = self.scan(fast=False)
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(self.requests, ["https://www.instagram.com/", "https://www.instagram.com/owner/",
                                        "https://www.instagram.com/owner/"])

    def test_collects_initial_and_later_virtualized_rows_with_paced_scrolling(self):
        self.fixture = profile_fixture(["alice"], ["bob"], follower_count="3")
        next_rows = json.dumps([row("carol"), row("dave")])
        self.fixture = self.fixture.replace("</script>", f'''
          window.scrolls=Number(sessionStorage.getItem('scrolls')||0);const later={next_rows};
          const users=document.querySelector('#followers .users');
          users.insertAdjacentHTML('beforeend','<div style="height:600px"></div>');
          users.addEventListener('scroll',()=>{{
            if(scrolls<later.length){{users.innerHTML=later[scrolls]+'<div style="height:600px"></div>';scrolls++;sessionStorage.setItem('scrolls',String(scrolls));}}
          }});</script>''')
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.followers, ("alice", "carol", "dave"))
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertGreaterEqual(self.driver.now, 10)
        self.assertEqual(self.page.evaluate("scrolls"), 2)
        self.assertEqual(self.driver.listeners, [])

    def test_missing_rows_stall_without_exposing_negative_results(self):
        self.fixture = profile_fixture(["alice"], ["bob"], follower_count="2")
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.not_following_back, ())
        self.assertGreaterEqual(self.driver.now, 5)
        self.assertLess(self.driver.now, 60)

    def test_stop_during_scroll_wait_returns_promptly_and_detaches_listener(self):
        self.fixture = profile_fixture(["alice"], ["bob"], follower_count="2")
        stop = Event()
        result = self.scan(stop, on_wait=lambda driver: stop.set() if driver.now >= 2 else None)
        self.assertEqual(result.status, "stopped")
        self.assertLessEqual(self.driver.now, 2.25)
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.driver.listeners, [])

    def test_instagram_auth_and_rate_responses_stop_without_opening_lists(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.response_status = status
                result = self.scan()
                self.assertEqual(result.status, "restricted")
                self.assertEqual(result.not_following_back, ())
                self.assertEqual(self.page.evaluate("actions"), [])
                self.assertEqual(self.driver.listeners, [])

    def test_visible_english_and_turkish_restrictions_stop_before_navigation(self):
        for message in ("Try again later", "We restrict certain activity", "Kimliğini doğrula",
                        "Daha sonra tekrar dene", "Bir sorun oluştu"):
            with self.subTest(message=message):
                self.fixture = profile_fixture(["alice"], ["bob"]) + f'<div role="alert">{message}</div>'
                self.requests.clear()
                result = self.scan()
                self.assertEqual(result.status, "restricted")
                self.assertEqual(self.requests, ["https://www.instagram.com/"])
                self.assertEqual(result.not_following_back, ())

    def test_login_challenge_and_checkpoint_urls_stop_before_navigation(self):
        for suffix in ("accounts/login/", "challenge/", "checkpoint/"):
            with self.subTest(suffix=suffix):
                self.page.goto("https://www.instagram.com/" + suffix)
                self.driver = FastPage(self.page)
                result = non_followers.scan_non_followers(self.driver, Event())
                self.assertEqual(result.status, "restricted")
                self.assertEqual(self.page.url, "https://www.instagram.com/" + suffix)
                self.assertEqual(self.driver.listeners, [])

    def test_scroll_limit_is_shared_by_both_lists(self):
        self.fixture = profile_fixture(["f0"], ["g0"], follower_count="65", following_count="65")
        self.fixture = self.fixture.replace("</script>", '''
          window.scrolls=0;
          for(const kind of ['followers','following']) {
            const users=document.querySelector('#'+kind+' .users');let index=0;
            users.insertAdjacentHTML('beforeend','<div style="height:600px"></div>');
            users.addEventListener('scroll',()=>{
              index++;scrolls++; const username=(kind==='followers'?'f':'g')+index;
              users.insertAdjacentHTML('beforeend','<div role="listitem" style="height:60px"><a href="/'+username+'/">'+username+'</a><button>Following</button></div>');
            });
          }</script>''')
        with patch.object(non_followers, "_MAX_SCROLLS", 120):
            result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(self.page.evaluate("scrolls"), 120)
        self.assertEqual(len(result.followers), 65)
        self.assertEqual(len(result.following), 57)
        self.assertEqual(result.not_following_back, ())

    def test_session_deadline_stops_before_further_scrolls(self):
        self.fixture = profile_fixture(["alice"], ["bob"], follower_count="2")

        def pass_deadline(driver):
            driver.now = 901

        with patch.object(non_followers, "_MAX_SECONDS", 900):
            result = self.scan(on_wait=pass_deadline)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.page.evaluate("actions"), ["followers"])
        self.assertEqual(self.driver.listeners, [])

    def test_uppercase_english_relationship_labels_still_identify_div_rows(self):
        self.fixture = profile_fixture(["alice"], ["bob"]).replace(' role="listitem"', '')
        self.fixture = self.fixture.replace('>Following</button>', '>FOLLOWING</button>')
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.not_following_back, ("bob",))

    def test_external_page_profile_label_never_selects_an_instagram_account(self):
        self.page.goto("https://example.com/")
        driver = FastPage(self.page)
        result = non_followers.scan_non_followers(driver, Event())
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.username, "")
        self.assertEqual(self.requests, ["https://example.com/"])
        self.assertEqual(driver.listeners, [])

    def test_plain_visible_restriction_text_is_detected_but_hidden_text_is_ignored(self):
        self.fixture = profile_fixture(["alice"], ["bob"]) + '<div>Try again later</div>'
        result = self.scan()
        self.assertEqual(result.status, "restricted")
        self.fixture = profile_fixture(["alice"], ["bob"]) + '<div style="display:none">Try again later</div>'
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)

    def test_navigation_timeout_is_not_retried_and_detaches_listener(self):
        class FailedNavigation(FastPage):
            attempts = 0

            def goto(self, *args, **kwargs):
                self.attempts += 1
                raise PWTimeout("offline slow navigation")

        self.page.goto("https://www.instagram.com/")
        driver = FailedNavigation(self.page)
        result = non_followers.scan_non_followers(driver, Event())
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(driver.attempts, 1)
        self.assertEqual(driver.listeners, [])

    def test_missing_dialog_is_not_reopened(self):
        self.fixture = profile_fixture(["alice"], ["bob"])
        self.fixture = self.fixture.replace("document.getElementById(name).style.display='block';", "")
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(self.page.evaluate("actions"), ["followers"])
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.driver.listeners, [])

    def test_stop_before_scan_does_not_navigate_to_profile(self):
        stop = Event()
        stop.set()
        result = self.scan(stop)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(self.requests, ["https://www.instagram.com/"])
        self.assertEqual(self.driver.listeners, [])

    def test_suggestion_section_aria_label_excludes_nested_semantic_user_rows(self):
        self.fixture = profile_fixture(["alice"], ["bob"], follower_count="2",
            follower_extra='<section aria-label="Suggested for you">' + row("bob") + '</section>')
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.not_following_back, ())

    def test_cross_scheme_profile_list_link_is_not_opened(self):
        self.fixture = profile_fixture(["alice"], ["bob"])
        self.fixture = self.fixture.replace('href="/owner/followers/"',
                                            'href="http://www.instagram.com/owner/followers/"')
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(self.page.evaluate("actions"), [])

    def test_disagreeing_exact_title_and_displayed_count_are_ambiguous(self):
        self.fixture = profile_fixture(["alice", "carol"], ["bob"])
        self.fixture = self.fixture.replace('id="followers-link"', 'id="followers-link" title="1"')
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertIsNone(result.expected_followers)
        self.assertEqual(self.page.evaluate("actions"), [])

    def test_fresh_own_profile_counts_must_match_when_initial_dom_stays_stale(self):
        self.fixture = profile_fixture(["alice"], ["bob"])
        self.refresh_fixture = profile_fixture(["alice", "carol"], ["bob"])
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.owner_visits, 2)
        self.assertEqual(self.driver.listeners, [])

    def test_rate_response_during_final_profile_refresh_suppresses_negatives(self):
        self.refresh_status = 429
        result = self.scan()
        self.assertEqual(result.status, "restricted")
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.owner_visits, 2)
        self.assertEqual(self.driver.listeners, [])

    def test_dialog_without_labeled_close_button_can_close_with_escape(self):
        self.fixture = profile_fixture(["alice"], ["bob"]).replace('aria-label="Close"', '')
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertEqual(self.owner_visits, 2)

    def test_own_profile_from_textless_navigation_image_alt(self):
        # Ignoring img alt breaks compact Instagram navigation with no Profile text.
        for container, alt in (("nav", "owner's profile picture"),
                               ("aside", "owner profil resmi"),
                               ("navigation", "owner adlı kişinin profil resmi")):
            with self.subTest(container=container, alt=alt):
                self.requests.clear()
                self.fixture = profile_fixture(["alice"], ["bob"],
                    sidebar=compact_sidebar(profile_picture_link(alt=alt), container=container))
                result = self.scan()
                self.assertEqual(result.status, "completed", result.detail)
                self.assertEqual(result.username, "owner")
                self.assertEqual(result.not_following_back, ("bob",))
                self.assertEqual(self.requests, ["https://www.instagram.com/",
                                                "https://www.instagram.com/owner/",
                                                "https://www.instagram.com/owner/"])

    def test_own_profile_from_textless_div_sidebar_with_home_and_other_navigation(self):
        for other_route, alt in (("/reels/", "owner's profile picture"),
                                 ("/direct/inbox/", "owner'in profil resmi")):
            with self.subTest(other_route=other_route, alt=alt):
                self.fixture = profile_fixture(["alice"], ["bob"],
                    sidebar=compact_sidebar(profile_picture_link(alt=alt), container="div", other_route=other_route))
                result = self.scan()
                self.assertEqual(result.status, "completed", result.detail)
                self.assertEqual(result.username, "owner")
                self.assertEqual(result.not_following_back, ("bob",))

    def test_own_profile_div_sidebar_uses_local_navigation_ancestor_not_page_body(self):
        # Reproduce the observed 48px sidebar with its avatar five ancestors
        # below a DIV containing Home/Reels/Inbox/Explore. Right-side avatars
        # may share the same alt phrase, but only body contains their nav peers.
        sidebar = ('<div style="position:fixed;left:0;top:0;width:48px;height:700px">'
                   '<div><a href="/"><svg width="24" height="24" aria-label="Home"></svg></a></div>'
                   '<div><a href="/reels/"><svg width="24" height="24" aria-label="Reels"></svg></a></div>'
                   '<div><a href="/direct/inbox/"><svg width="24" height="24" aria-label="Messages"></svg></a></div>'
                   '<div><a href="/explore/"><svg width="24" height="24" aria-label="Explore"></svg></a></div>'
                   '<div><div><div><div>' + profile_picture_link(alt="owner'in profil resmi")
                   + '</div></div></div></div></div>')
        outside = ('<div style="position:fixed;right:0;top:0;width:320px">'
                   + profile_picture_link(alt="owner'in profil resmi")
                   + profile_picture_link("other", "other'in profil resmi") + '</div>')
        self.fixture = profile_fixture(["alice"], ["bob"], sidebar=sidebar + outside)
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.username, "owner")
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertEqual(self.requests, ["https://www.instagram.com/",
                                        "https://www.instagram.com/owner/",
                                        "https://www.instagram.com/owner/"])

    def test_own_profile_avatar_in_main_story_or_right_suggestion_is_rejected(self):
        picture = profile_picture_link()
        unrelated = (f'<main><section>{picture}</section></main>',
                     f'<div aria-label="Stories">{picture}</div>',
                     f'<aside aria-label="Suggestions for you" style="position:fixed;right:0;width:320px">'
                     f'<h3>Suggested for you</h3>{picture}</aside>',
                     f'<div style="position:fixed;right:0;width:320px">{picture}</div>')
        for content in unrelated:
            with self.subTest(content=content):
                self.requests.clear()
                self.fixture = profile_fixture(["alice"], ["bob"], sidebar=content)
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.username, "")
                self.assertEqual(result.not_following_back, ())
                self.assertEqual(self.requests, ["https://www.instagram.com/"])

    def test_own_profile_avatar_needs_both_home_and_separate_navigation_evidence(self):
        picture = profile_picture_link()
        for content in (f'<nav>{picture}</nav>',
                        f'<aside><a href="/"><svg aria-label="Home"></svg></a>{picture}</aside>',
                        f'<nav><a href="/reels/"><svg aria-label="Reels"></svg></a>{picture}</nav>'):
            with self.subTest(content=content):
                self.requests.clear()
                self.fixture = profile_fixture(["alice"], ["bob"], sidebar=content)
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.username, "")
                self.assertEqual(self.requests, ["https://www.instagram.com/"])

    def test_own_profile_avatar_alt_must_name_the_linked_account(self):
        self.fixture = profile_fixture(["alice"], ["bob"],
            sidebar=compact_sidebar(profile_picture_link(alt="other's profile picture")))
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.username, "")
        self.assertEqual(self.requests, ["https://www.instagram.com/"])

    def test_own_profile_avatar_alt_requires_username_and_profile_picture_phrase(self):
        for alt in ("", "owner", "avatar", "profile picture", "owner holiday picture"):
            with self.subTest(alt=alt):
                self.requests.clear()
                self.fixture = profile_fixture(["alice"], ["bob"],
                    sidebar=compact_sidebar(profile_picture_link(alt=alt)))
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.username, "")
                self.assertEqual(self.requests, ["https://www.instagram.com/"])

    def test_own_profile_conflicting_sidebar_account_evidence_never_navigates(self):
        for own_link, other_link in (
            (profile_picture_link(), '<a href="/other/"><svg aria-label="Profile"></svg></a>'),
            ('<a href="/owner/">Profile</a>', profile_picture_link("other", "other's profile picture")),
            (profile_picture_link(), profile_picture_link("other", "other profil resmi")),
        ):
            with self.subTest(own_link=own_link, other_link=other_link):
                self.requests.clear()
                self.fixture = profile_fixture(["alice"], ["bob"],
                    sidebar=compact_sidebar(own_link, extra=other_link))
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.username, "")
                self.assertEqual(result.not_following_back, ())
                self.assertEqual(self.requests, ["https://www.instagram.com/"])

    def test_own_profile_sidebar_that_appears_after_spa_rendering_is_waited_for(self):
        self.fixture = profile_fixture(["alice"], ["bob"],
            sidebar='<aside id="own-sidebar" style="display:none"><a href="/owner/">Profile</a></aside>')

        def reveal_sidebar(driver):
            if driver.now >= .3:
                driver.page.evaluate("document.getElementById('own-sidebar').style.display='block'")

        result = self.scan(on_wait=reveal_sidebar)
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.username, "owner")
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertGreaterEqual(self.driver.now, .3)
        self.assertEqual(self.owner_visits, 2)

    def test_own_profile_initial_wait_can_stop_before_identity_appears(self):
        self.fixture = profile_fixture(["alice"], ["bob"], sidebar="")
        stop = Event()
        result = self.scan(stop, on_wait=lambda driver: stop.set() if driver.now >= .2 else None)
        self.assertEqual(result.status, "stopped")
        self.assertLessEqual(self.driver.now, .5)
        self.assertEqual(self.requests, ["https://www.instagram.com/"])
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.driver.listeners, [])

    def test_own_profile_initial_wait_honors_visible_restriction(self):
        self.fixture = profile_fixture(["alice"], ["bob"], sidebar="")

        def show_restriction(driver):
            if driver.now >= .2:
                driver.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=alert>Try again later</div>')")

        result = self.scan(on_wait=show_restriction)
        self.assertEqual(result.status, "restricted")
        self.assertLessEqual(self.driver.now, .5)
        self.assertEqual(self.requests, ["https://www.instagram.com/"])
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.driver.listeners, [])

    def test_own_profile_initial_wait_honors_instagram_rate_response(self):
        self.fixture = profile_fixture(["alice"], ["bob"], sidebar="")
        self.context.route("**/own-profile-rate/", lambda route: route.fulfill(status=429, body="blocked"))
        sent = False

        def receive_restriction(driver):
            nonlocal sent
            if driver.now >= .2 and not sent:
                sent = True
                driver.page.evaluate("fetch('/own-profile-rate/').catch(()=>{})")

        result = self.scan(on_wait=receive_restriction)
        self.assertEqual(result.status, "restricted")
        self.assertEqual(self.owner_visits, 0)
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.driver.listeners, [])

    def test_own_profile_initial_wait_expires_without_visiting_other_profiles(self):
        self.fixture = profile_fixture(["alice"], ["bob"], sidebar="")
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.username, "")
        self.assertEqual(result.not_following_back, ())
        self.assertGreaterEqual(self.driver.now, 1)
        self.assertLessEqual(self.driver.now, 10.25)
        self.assertEqual(self.requests, ["https://www.instagram.com/"])
        self.assertEqual(self.driver.listeners, [])

    def test_own_profile_turkish_hash_role_links_open_the_correct_two_lists(self):
        self.fixture = hash_profile_stats(profile_fixture(["alice"], ["bob"]))
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.username, "owner")
        self.assertEqual((result.expected_followers, result.expected_following), (1, 1))
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.following, ("bob",))
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertEqual(self.page.evaluate("actions"), ["followers", "following"])
        self.assertEqual(self.requests, ["https://www.instagram.com/",
                                        "https://www.instagram.com/owner/",
                                        "https://www.instagram.com/owner/"])

    def test_own_profile_hash_stats_use_observed_exact_turkish_grouped_counts(self):
        self.fixture = hash_profile_stats(profile_fixture(["alice"], ["bob"]),
            follower_label='<span title="6.092">6.092</span> takipçi', following_label='5.121 takip')
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual((result.expected_followers, result.expected_following), (6092, 5121))
        self.assertEqual(result.not_following_back, ())

    def test_own_profile_hash_stats_with_unknown_or_repeated_labels_do_not_open_lists(self):
        for followers, following in (("1 gönderi", "1 takip"),
                                     ("1 takipçi", "1 kişi"),
                                     ("1 takipçi", "1 takipçi")):
            with self.subTest(followers=followers, following=following):
                self.fixture = hash_profile_stats(profile_fixture(["alice"], ["bob"]),
                    follower_label=followers, following_label=following)
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.not_following_back, ())
                self.assertEqual(self.page.evaluate("actions"), [])

    def test_own_profile_duplicate_hash_stats_are_ambiguous_even_when_counts_agree(self):
        for extra in ('<a href="#" role="link"><span title="1">1</span> takipçi</a>',
                      '<a href="#" role="link">1 takip</a>'):
            with self.subTest(extra=extra):
                self.fixture = hash_profile_stats(profile_fixture(["alice"], ["bob"], header_extra=extra))
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.not_following_back, ())
                self.assertEqual(self.page.evaluate("actions"), [])

    def test_own_profile_hash_stats_with_ambiguous_count_do_not_compare(self):
        for followers in ("1,2 B takipçi", "1.23 takipçi", "yaklaşık 12 takipçi"):
            with self.subTest(followers=followers):
                self.fixture = hash_profile_stats(profile_fixture(["alice"], ["bob"]), follower_label=followers)
                result = self.scan()
                self.assertEqual(result.status, "incomplete")
                self.assertIsNone(result.expected_followers)
                self.assertEqual(result.not_following_back, ())
                self.assertEqual(self.page.evaluate("actions"), [])

    def test_own_profile_hash_stats_require_the_verified_own_header(self):
        self.fixture = hash_profile_stats(profile_fixture(["alice"], ["bob"]))
        self.fixture = self.fixture.replace('<h2>owner</h2>', '<h2>other</h2>')
        result = self.scan()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.not_following_back, ())
        self.assertEqual(self.page.evaluate("actions"), [])

    def test_own_profile_follower_div_remove_control_is_read_without_clicking(self):
        self.fixture = profile_fixture(["alice"], ["bob"])
        follower_row = ('<div style="height:55px"><a href="/alice/"><img width="32" height="32" alt="alice"></a>'
                        '<div><a href="/alice/">alice</a></div>'
                        '<div role="button" tabindex="0" onclick="actions.push(\'remove\')">Çıkar</div></div>')
        following_row = row("bob").replace(' role="listitem"', '').replace('>Following</button>', '>Takiptesin</button>')
        self.fixture = self.fixture.replace(row("alice"), follower_row).replace(row("bob"), following_row)
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.followers, ("alice",))
        self.assertEqual(result.following, ("bob",))
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertEqual(self.page.evaluate("actions"), ["followers", "following"])
        self.assertEqual(self.driver.listeners, [])

    def test_own_profile_dialog_close_button_named_by_child_svg_is_used(self):
        self.fixture = profile_fixture(["alice"], ["bob"], escape_close=False)
        original_button = '<button aria-label="Close" onclick="closeList(this.parentElement)">Close</button>'
        observed_button = ('<button onclick="actions.push(\'close\');closeList(this.parentElement)">'
                           '<svg width="24" height="24" aria-label="Kapat"></svg></button>')
        self.fixture = self.fixture.replace(original_button, observed_button)
        result = self.scan()
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.not_following_back, ("bob",))
        self.assertEqual(self.page.evaluate("actions"), ["followers", "close", "following", "close"])
        self.assertEqual(self.requests, ["https://www.instagram.com/",
                                        "https://www.instagram.com/owner/",
                                        "https://www.instagram.com/owner/"])
        self.assertEqual(self.driver.listeners, [])

    def test_scroll_movement_through_loaded_batch_is_not_a_missing_rows_stall(self):
        # A rendered batch spans many viewports. Counting all its loaded DOM
        # anchors immediately must not stop scrolling after three unchanged
        # counts, before the bottom can trigger the next batch.
        self.fixture = loaded_batch_fixture()
        result = self.scan()
        stats = self.page.evaluate("scrollStats")
        self.assertEqual(result.status, "completed",
                         f"{result.detail}; scrolls={stats}; read={len(result.followers)}/{len(result.following)}")
        self.assertEqual(len(result.followers), 42)
        self.assertEqual(len(result.following), 42)
        self.assertEqual(result.not_following_back, ("outsider",))
        self.assertGreater(stats["followers"]["events"], 3)
        self.assertGreater(stats["following"]["events"], 3)
        self.assertTrue(stats["followers"]["appended"])
        self.assertTrue(stats["following"]["appended"])
        self.assertEqual(self.page.evaluate("actions"), ["followers", "following"])
        self.assertEqual(self.requests, ["https://www.instagram.com/",
                                        "https://www.instagram.com/owner/",
                                        "https://www.instagram.com/owner/"])
        self.assertEqual(self.driver.listeners, [])

    def test_long_scan_completes_beyond_old_scroll_and_time_limits_with_five_second_pacing(self):
        self.fixture = loaded_batch_fixture(row_height=800)

        def record_virtual_time(driver):
            driver.page.evaluate("value => window.virtualScanTime=value", driver.now)

        result = self.scan(on_wait=record_virtual_time)
        stats = self.page.evaluate("scrollStats")
        total_scrolls = stats["followers"]["events"] + stats["following"]["events"]
        self.assertEqual(result.status, "completed",
                         f"{result.detail}; scrolls={total_scrolls}; seconds={self.driver.now}; read={len(result.followers)}/{len(result.following)}")
        self.assertEqual(len(result.followers), 42)
        self.assertEqual(len(result.following), 42)
        self.assertEqual(result.not_following_back, ("outsider",))
        self.assertGreater(total_scrolls, 120)
        self.assertGreater(self.driver.now, 900)
        self.assertTrue(stats["followers"]["appended"])
        self.assertTrue(stats["following"]["appended"])
        for kind in ("followers", "following"):
            times = stats[kind]["times"]
            self.assertEqual(len(times), stats[kind]["events"])
            for earlier, later in zip(times, times[1:]):
                self.assertGreaterEqual(later - earlier, 4.99, f"{kind}: {earlier} -> {later}")
        self.assertEqual(self.page.evaluate("actions"), ["followers", "following"])
        self.assertEqual(self.requests, ["https://www.instagram.com/",
                                        "https://www.instagram.com/owner/",
                                        "https://www.instagram.com/owner/"])
        self.assertEqual(self.driver.listeners, [])


if __name__ == "__main__":
    unittest.main()
