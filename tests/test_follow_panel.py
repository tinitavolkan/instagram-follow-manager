"""Legacy follow-tab adapter regression, isolated from real data and Instagram."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FollowPanelTests(unittest.TestCase):
    def test_isolated_panel_controls_shared_worker_and_legacy_persistence(self):
        with TemporaryDirectory(prefix="follow-panel-") as folder:
            env = dict(os.environ, INSTAGRAMTAKIP_DATA_DIR=folder)
            completed = subprocess.run([sys.executable, "-B", "-X", "utf8", __file__, "--isolated"],
                                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=40)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("FOLLOW_PANEL_OK", completed.stdout)


def isolated():
    import json
    import queue
    import socket
    import threading
    import time
    from datetime import date
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    import customtkinter as ctk

    folder = Path(os.environ["INSTAGRAMTAKIP_DATA_DIR"])
    today = str(date.today())
    files = {
        "Takiplist.txt": "follow_target\nold_exclusion\n",
        "karalist.txt": "old_exclusion\n",
        "daily_stats.json": json.dumps({"date": today, "count": 7}),
        "stats.json": json.dumps({"date": "2000-01-01", "success": 4, "skipped": 2, "errors": 1}),
        "daily_history.json": json.dumps([{"date": "2000-01-01", "success": 4, "skipped": 2, "errors": 1}]),
        "config.json": json.dumps({"DAILY_LIMIT": 80, "HEADLESS_MODE": True,
                                    "GONDERIDEN_EXCLUDE_FOLLOWING": True,
                                    "unknown_setting_marker": {"preserve": True}}),
    }
    for name, content in files.items():
        (folder / name).write_text(content, encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    try:
        from instagram_bot.follow_panel import LegacyFollowPanel
    except ModuleNotFoundError as exc:
        raise AssertionError("LegacyFollowPanel is not implemented") from exc
    from instagram_bot import browser, worker, config
    from instagram_bot.following import FollowResult
    from instagram_bot.dashboard import reported_wait

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.geometry("1050x680")
            self.closing = False
            self.jobs, self.messages = queue.Queue(), []
            self.callback = None
            self.controller = SimpleNamespace(busy=False, follow=self.follow, open_session=lambda: True,
                                              stop=lambda: self.shared_stop.set())
            self.shared_stop, self.shared_close = threading.Event(), threading.Event()

        def follow(self, callback):
            self.callback = callback
            return True

        def _run_command(self, command, detail):
            if not self.controller.busy and command():
                self.controller.busy = True
                self.panel.set_busy(True)

        def call_in_main_thread(self, callback, *args, **kwargs):
            self.jobs.put((callback, args, kwargs))

        def log(self, message):
            self.messages.append(message)

        def drain(self):
            while not self.jobs.empty():
                callback, args, kwargs = self.jobs.get_nowait()
                callback(*args, **kwargs)
            self.update()

    with patch.object(socket.socket, "connect", side_effect=AssertionError("follow-panel test must stay offline")):
        app = App()
        try:
            app.panel = panel = LegacyFollowPanel(app, app)
            panel.pack(fill="both", expand=True)
            app.drain()
            assert "follow_target" in panel.listbox.get("1.0", "end")
            for name in ("follow_btn", "start_btn", "stop_btn", "settings_btn", "history_btn", "user_status_btn"):
                assert hasattr(panel, name), name
            for name, content in files.items():
                assert (folder / name).read_text(encoding="utf-8") == content, "Opening must not rewrite " + name
            for opener in (panel.open_history_window, panel.open_user_status_window):
                window = opener()
                app.drain()
                assert window.winfo_exists()
                window.destroy()
            panel.open_settings()
            app.drain()
            panel.set_busy(True)
            assert panel.follow_btn.cget("state") == "disabled"
            assert panel.start_btn.cget("state") == "disabled"
            assert panel.settings_btn.cget("state") == "disabled"
            assert panel.daily_limit_entry.cget("state") == "disabled"
            assert panel.settings_save_btn.cget("state") == "disabled"
            panel.set_busy(False)
            panel.daily_limit_entry.delete(0, "end")
            panel.daily_limit_entry.insert(0, "120")
            panel.settings_save_btn.invoke()
            assert config.CONFIG["DAILY_LIMIT"] == 120
            assert config.CONFIG["HEADLESS_MODE"] is True, "Unrelated saved settings must be preserved"
            assert json.loads((folder / "config.json").read_text(encoding="utf-8"))["unknown_setting_marker"] == {"preserve": True}
            for name, content in files.items():
                if name == "config.json":
                    continue  # The explicit Save above intentionally changed the daily limit.
                assert (folder / name).read_text(encoding="utf-8") == content, "Read-only controls rewrote " + name

            def run(action):
                app.controller.busy = False
                panel.set_busy(False)
                app.shared_stop.clear()
                panel.follow_btn.invoke()
                assert app.callback is not None
                outcomes, errors = [], []
                shared_page = SimpleNamespace(is_closed=lambda: False)
                sentinel = object()
                previous = browser.page
                browser.page = sentinel
                original_core = worker.WorkerMixin._run_follow_task

                def observed_action(username, cb=None, stop_event=None):
                    assert threading.current_thread() is not threading.main_thread()
                    assert browser.page is shared_page
                    assert stop_event is app.shared_stop
                    with reported_wait(5, "Örnek güvenli mola", stop_event=stop_event):
                        assert panel.dashboard.snapshot()["state"] == "waiting", "Legacy reporting scope is missing"
                    return action(username)

                def execute():
                    try:
                        outcomes.append(app.callback(shared_page, app.shared_stop, app.shared_close))
                    except BaseException as exc:
                        errors.append(exc)

                try:
                    with patch.object(browser, "run_bot", side_effect=AssertionError("No credential login")) as login, \
                         patch.object(browser, "perform_safe_wait", return_value=False), \
                         patch.object(worker.random, "random", return_value=1), \
                         patch.object(worker, "follow_user", side_effect=observed_action), \
                         patch.object(worker.WorkerMixin, "_run_follow_task", autospec=True, side_effect=original_core) as core, \
                         patch.object(panel, "_wait_with_status", return_value=False), \
                         patch.object(panel, "_wait_briefly", return_value=False):
                        thread = threading.Thread(target=execute)
                        thread.start()
                        while thread.is_alive():
                            app.drain()
                            thread.join(.01)
                        app.drain()
                        assert not errors, errors
                        assert core.call_count == 1
                        login.assert_not_called()
                    assert browser.page is sentinel, "Shared legacy page binding was not restored"
                finally:
                    browser.page = previous
                assert panel.active_task is None
                assert not panel.follow_pending
                app.controller.busy = False
                panel.set_busy(False)
                return outcomes[0]

            # Entry protection belongs to the adapter because its legacy worker_loop is not used.
            with patch.object(browser, "rate_limit_lock_until", time.time() + 100), \
                 patch.object(worker.WorkerMixin, "_run_follow_task") as core:
                outcome = panel.run_follow(SimpleNamespace(is_closed=lambda: False), app.shared_stop, app.shared_close)
                assert outcome["state"] == "attention"
                assert core.call_count == 0
                assert panel.active_task is None and not panel.follow_pending
            (folder / "Takiplist.txt").write_text("follow_target\n", encoding="utf-8")
            panel.refresh()
            outcome = run(lambda username: FollowResult("followed", "profile_relationship_confirmed", action_submitted=True))
            assert outcome["state"] == "idle", outcome
            assert (folder / "Takiplist.txt").read_text(encoding="utf-8").strip() == ""
            assert "follow_target" in (folder / "karalist.txt").read_text(encoding="utf-8")
            assert panel.limiter.count == 8
            assert panel.progress_bar.get() == 1
            assert any(item["date"] == "2000-01-01" for item in panel.stats.history.records)

            (folder / "Takiplist.txt").write_text("retry_target\n", encoding="utf-8")
            panel.refresh()
            attempts = []
            def retry(username):
                attempts.append(username)
                return FollowResult("retryable_error", "profile_timeout")
            outcome = run(retry)
            assert attempts == ["retry_target"] * 3, attempts
            assert outcome["state"] == "attention", outcome
            assert "retry_target" in (folder / "Takiplist.txt").read_text(encoding="utf-8")

            (folder / "Takiplist.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            panel.refresh()
            attempts.clear()
            def unconfirmed(username):
                attempts.append(username)
                return FollowResult("unconfirmed", "verification_timeout", action_submitted=True)
            outcome = run(unconfirmed)
            assert len(attempts) == 3
            assert panel.follow_safety_stop
            assert outcome["state"] == "attention", outcome
            assert len((folder / "Takiplist.txt").read_text(encoding="utf-8").splitlines()) == 4
            assert not (folder / "idsifre.txt").exists()
            print("FOLLOW_PANEL_OK: preserved files, controls, settings lock, shared page/thread, legacy follow/retries/safety")
        finally:
            app.closing = True
            app.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["--isolated"]:
        isolated()
    else:
        unittest.main()
