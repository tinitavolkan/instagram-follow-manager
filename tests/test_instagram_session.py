"""Session lifecycle at the browser-launch boundary; no live browser/login."""
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from instagram_bot.instagram_session import InstagramSession


class Page:
    def __init__(self, state):
        self.state = state

    def is_closed(self):
        return self.state["closed"]

    def goto(self, url, **kwargs):
        self.state["visits"].append(url)
        if self.state.get("navigation_error"):
            raise RuntimeError("offline navigation failure")


class Context:
    def __init__(self, state):
        self.state = state
        self.pages = [Page(state)]

    def close(self):
        self.state["context_closed"] += 1
        self.state["closed"] = True


class Runtime:
    def __init__(self, state, stop_on_start=None):
        self.state, self.stop_on_start = state, stop_on_start
        self.chromium = SimpleNamespace(launch_persistent_context=self.launch)

    def start(self):
        self.state["starts"] += 1
        if self.stop_on_start:
            self.stop_on_start.set()
        return self

    def launch(self, **kwargs):
        self.state["launches"].append(kwargs)
        self.state["closed"] = False
        return Context(self.state)

    def stop(self):
        self.state["stops"] += 1


class InstagramSessionTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(starts=0, stops=0, context_closed=0, closed=False, visits=[], launches=[])
        self.runtime = Runtime(self.state)
        self.session = InstagramSession(Path("offline-test-profile"))
        patcher = patch("instagram_bot.instagram_session.sync_playwright", return_value=self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.session.close)

    def test_visible_persistent_open_reuses_page_without_second_navigation(self):
        self.assertIsNone(self.session.page)
        page = self.session.open()
        self.assertIs(self.session.open(), page)
        self.assertEqual(self.state["visits"], ["https://www.instagram.com/"])
        self.assertEqual(len(self.state["launches"]), 1)
        options = self.state["launches"][0]
        self.assertEqual(options["user_data_dir"], str(Path("offline-test-profile").resolve()))
        self.assertFalse(options["headless"])
        self.session.close()
        self.assertIsNone(self.session.page)
        self.assertEqual((self.state["context_closed"], self.state["stops"]), (1, 1))

    def test_stopped_open_does_not_launch(self):
        stop = Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            self.session.open(stop)
        self.assertEqual(self.state["launches"], [])
        self.assertIsNone(self.session.page)

    def test_stop_during_runtime_start_prevents_browser_launch_and_cleans_runtime(self):
        stop = Event()
        self.runtime.stop_on_start = stop
        with self.assertRaises(InterruptedError):
            self.session.open(stop)
        self.assertEqual(self.state["launches"], [])
        self.assertEqual(self.state["stops"], 1)
        self.assertIsNone(self.session.page)

    def test_navigation_failure_cleans_both_resources(self):
        self.state["navigation_error"] = True
        with self.assertRaises(RuntimeError):
            self.session.open()
        self.assertIsNone(self.session.page)
        self.assertEqual((self.state["context_closed"], self.state["stops"]), (1, 1))

    def test_cross_thread_open_and_close_are_rejected(self):
        original = self.session.open()
        failures = []

        def elsewhere():
            for operation in (self.session.open, self.session.close):
                try:
                    operation()
                except RuntimeError as exc:
                    failures.append(str(exc))

        thread = Thread(target=elsewhere)
        thread.start()
        thread.join()
        self.assertEqual(len(failures), 2)
        self.assertIs(self.session.page, original)
        self.assertEqual(self.state["context_closed"], 0)


if __name__ == "__main__":
    unittest.main()
