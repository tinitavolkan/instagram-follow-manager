"""Offline integration regressions for actual worker, storage and GUI methods.

The package is imported under an isolated name and temporary data directory.
Every test uses real list/SQLite persistence, fake browser results and a
deterministic clock. Credentials and existing application data are never read.
"""
from collections import Counter
from contextlib import closing
import importlib
import importlib.util
import os
from pathlib import Path
import queue
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PACKAGE_NAME = "_offline_resume_fixture"
ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    global package_folder, worker, gui, storage, FollowResult
    package_folder = TemporaryDirectory(prefix="instagramtakip-import-test-")
    # A unique package name also isolates us when another test already imported
    # instagram_bot.paths before our temporary environment was established.
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, ROOT / "instagram_bot" / "__init__.py",
        submodule_search_locations=[str(ROOT / "instagram_bot")],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    with patch.dict(os.environ, {"INSTAGRAMTAKIP_DATA_DIR": package_folder.name}):
        spec.loader.exec_module(package)
        worker = importlib.import_module(f"{PACKAGE_NAME}.worker")
        gui = importlib.import_module(f"{PACKAGE_NAME}.gui")
        storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
        FollowResult = importlib.import_module(f"{PACKAGE_NAME}.following").FollowResult


def tearDownModule():
    logger = importlib.import_module(f"{PACKAGE_NAME}.logging_setup").APP_LOG
    for handler in list(logger.handlers):
        filename = getattr(handler, "baseFilename", "")
        if filename and Path(filename).resolve().parent == Path(package_folder.name).resolve():
            handler.close()
            logger.removeHandler(handler)
    for name in list(sys.modules):
        if name == PACKAGE_NAME or name.startswith(PACKAGE_NAME + "."):
            del sys.modules[name]
    package_folder.cleanup()


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.wait_calls = 0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.wait_calls += 1
        if self.wait_calls > 10000:
            raise AssertionError("Unexpectedly unbounded wait/retry loop")
        self.now += max(0, seconds)


class Event:
    def __init__(self, clock):
        self.clock = clock
        self.flag = False
        self.on_wait = None

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout=None):
        if self.on_wait is not None:
            self.on_wait()
        if not self.flag:
            self.clock.sleep(timeout or 0)
        return self.flag


class BoundedQueue(queue.Queue):
    def get(self, block=True, timeout=None):
        try:
            return super().get(block=False)
        except queue.Empty as exc:
            raise AssertionError("Worker consumed all test tasks without exiting") from exc


class Limiter:
    def __init__(self):
        self.daily_blocked = False
        self.daily_reset_calls = 0
        self.marked = 0

    def daily_limit_reached(self):
        return self.daily_blocked

    def seconds_until_next_day(self):
        return 120

    def check_daily_reset(self):
        self.daily_reset_calls += 1
        self.daily_blocked = False

    def hourly_limit_reached(self):
        return False

    def mark_follow(self):
        self.marked += 1

    def next_delay(self):
        return 0


def run_follow(app, *subsequent_tasks):
    for task in ("follow", *subsequent_tasks, "shutdown"):
        app.task_queue.put(task)
    app.worker_loop()
    assert app.task_queue.unfinished_tasks == 0, "Every worker task must be acknowledged"
    assert app.active_task is None, "Worker must clear its active task on completion"


class FollowResumeTests(unittest.TestCase):
    def make_app(self, users=(), outcome=None):
        folder = TemporaryDirectory(prefix="instagramtakip-worker-test-")
        self.addCleanup(folder.cleanup)
        directory = Path(folder.name)
        paths = patch.multiple(storage, TAKIPLIST_PATH=directory / "Takiplist.txt",
                               KARALIST_PATH=directory / "karalist.txt",
                               STATUS_DB_PATH=directory / "statuses.sqlite3")
        paths.start()
        self.addCleanup(paths.stop)
        storage.save_takiplist(users)

        class Harness(worker.WorkerMixin):
            process_gui_queue = gui.InstaGUI.process_gui_queue
            call_in_main_thread = gui.InstaGUI.call_in_main_thread

        app = Harness()
        clock = Clock()
        app.directory = directory
        app.takipler = list(users)
        app.clock = clock
        app.dashboard = importlib.import_module(f"{PACKAGE_NAME}.dashboard").DashboardState(
            remaining=len(users), monotonic=clock.monotonic)
        app.stop_event = Event(clock)
        app.shutdown_event = Event(clock)
        app.active_task = None
        app.follow_pending = True
        app.follow_safety_stop = False
        app.closing = False
        app.task_queue = BoundedQueue()
        app.gui_queue = queue.Queue()
        app.logs = []
        app.log = lambda message: app.logs.append((clock.now, message))
        app._log_gui = app.log
        for name in ("after", "refresh_listbox", "update_stats_panel", "update_progress_bar",
                     "collect_likes", "collect_user_posts", "collect_post_links"):
            setattr(app, name, Mock())
        app._ensure_bot_ready = Mock(return_value=True)
        app.stats = SimpleNamespace(record_result=Mock(), record_delay=Mock(), record_error=Mock())
        app.limiter = Limiter()
        app.trigger_daily_limit_button = Mock(side_effect=AssertionError(
            "Daily resumption must not enqueue another follow task"))
        app.attempts = []
        counts = Counter()

        def fake_follow_user(user, callback, stop_event):
            app.attempts.append(user)
            counts[user] += 1
            result = outcome(user, counts[user]) if outcome else False
            if result is True:
                return FollowResult("followed", "follow_confirmed", action_submitted=True)
            if result is False:
                return FollowResult("retryable_error", "profile_timeout", "slow profile")
            return result

        app.safe_waits = []

        def safe_wait(seconds, callback=None, return_to_insta=False, stop_event=None):
            app.safe_waits.append(seconds)
            return stop_event.wait(seconds)

        app.browser = SimpleNamespace(page=None, rate_limit_lock_until=0, close_bot=Mock(),
                                      perform_safe_wait=safe_wait)
        app.namespace = {
            "time": clock, "APP_LOG": Mock(), "browser": app.browser,
            "random": SimpleNamespace(shuffle=lambda users: None, random=lambda: 1.0,
                                      uniform=lambda low, high: low),
            "write_to_karalist": Mock(wraps=storage.write_to_karalist),
            "remove_from_takiplist": Mock(wraps=storage.remove_from_takiplist),
            "read_takiplist": storage.read_takiplist,
            "record_user_status": Mock(wraps=storage.record_user_status),
            "stoppable_wait": lambda event, seconds: event.wait(seconds),
            "follow_user": fake_follow_user,
        }
        worker_patch = patch.multiple(worker, **app.namespace)
        worker_patch.start()
        self.addCleanup(worker_patch.stop)
        gui_patch = patch.object(gui, "APP_LOG", Mock())
        gui_patch.start()
        self.addCleanup(gui_patch.stop)
        app.waits = []
        original_wait = app._wait_with_status

        def record_wait(seconds, reason):
            app.waits.append((seconds, reason))
            return original_wait(seconds, reason)

        app._wait_with_status = record_wait
        return app

    def test_final_fifth_skip_retries_and_second_round_completes(self):
        users = [f"user{i}" for i in range(5)]
        app = self.make_app(users, outcome=lambda user, attempt: attempt > 1)
        run_follow(app)
        self.assertEqual(Counter(app.attempts), Counter({user: 2 for user in users}))
        self.assertEqual(app.takipler, [])
        self.assertEqual(app.limiter.marked, 5)
        self.assertTrue(any(seconds == 180 and "skip" in reason.lower()
                            for seconds, reason in app.waits))
        self.assertTrue(any("tamam" in message.lower() for _, message in app.logs))
        self.assertEqual(storage.read_takiplist(), [])
        self.assertEqual(set(storage.read_takiplist(storage.KARALIST_PATH)), set(users))
        self.assertTrue(all(row["status"] == "followed" and row["attempts"] == 2
                            for row in storage.get_user_statuses()))
        self.assertEqual(app.dashboard.snapshot()["state"], "completed")
        self.assertEqual(app.dashboard.snapshot()["remaining"], 0)
        self.assertEqual(app.dashboard.snapshot()["last_success_user"], "user4")

    def test_persistent_profile_timeout_keeps_users_and_reasons_after_three_rounds(self):
        users = [f"user{i}" for i in range(5)]
        app = self.make_app(users)
        run_follow(app)
        self.assertEqual(Counter(app.attempts), Counter({user: 3 for user in users}))
        self.assertEqual(app.takipler, users)
        self.assertEqual(storage.read_takiplist(), users)
        self.assertEqual(storage.read_takiplist(storage.KARALIST_PATH), [])
        app.namespace["write_to_karalist"].assert_not_called()
        self.assertEqual(app.limiter.marked, 0)
        statuses = storage.get_user_statuses()
        self.assertEqual(len(statuses), len(users))
        for row in statuses:
            self.assertEqual((row["status"], row["reason"], row["attempts"]),
                             ("retryable_error", "profile_timeout", 3))
            self.assertIn("slow profile", row["detail"])
        self.assertIn("3 takip turu tamamlandı", app.logs[-1][1])
        self.assertIn("Takip Et", app.logs[-1][1])
        self.assertIn("durduruldu", app.logs[-1][1])
        self.assertFalse(app.follow_pending)
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")
        self.assertEqual(app.dashboard.snapshot()["remaining"], 5)
        self.assertIsNone(app.dashboard.snapshot()["last_success_at"])

    def test_stop_during_last_skip_pause_does_not_retry(self):
        users = [f"user{i}" for i in range(5)]
        app = self.make_app(users)
        original_wait = app._wait_with_status

        def cancel_on_skip_pause(seconds, reason):
            if "skip" in reason.lower():
                app.stop_event.on_wait = app.stop_event.set
            return original_wait(seconds, reason)

        app._wait_with_status = cancel_on_skip_pause
        run_follow(app)
        self.assertTrue(app.stop_event.is_set())
        self.assertEqual(app.attempts, users)
        self.assertEqual(app.takipler, users)
        self.assertFalse(any("yeniden deneme" in reason.lower() for _, reason in app.waits))

    def test_stop_during_retry_cooldown_prevents_second_round(self):
        app = self.make_app(["user"])
        original_wait = app._wait_with_status

        def cancel_retry(seconds, reason):
            if "yeniden deneme" in reason.lower():
                app.stop_event.on_wait = app.stop_event.set
            return original_wait(seconds, reason)

        app._wait_with_status = cancel_retry
        run_follow(app)
        self.assertTrue(app.stop_event.is_set())
        self.assertEqual(app.attempts, ["user"])
        self.assertEqual(app.takipler, ["user"])
        self.assertTrue(any("iptal" in message for _, message in app.logs))

    def test_double_click_does_not_duplicate_task_or_clear_pending_stop(self):
        for state in ("queued", "active"):
            with self.subTest(state=state):
                app = self.make_app(["user"])
                app.follow_pending = False
                app.follow_clicked()
                self.assertEqual(list(app.task_queue.queue), ["follow"])
                if state == "active":
                    app.task_queue.get_nowait()
                    app.task_queue.task_done()
                    app.active_task = "follow"
                    app.follow_pending = False
                app.stop_event.set()
                queue_size = app.task_queue.qsize()
                app.follow_clicked()
                self.assertEqual(app.task_queue.qsize(), queue_size)
                self.assertTrue(app.stop_event.is_set())

    def test_terminal_invalid_username_is_excluded_with_reason_and_not_retried(self):
        app = self.make_app(["invalid/name"], outcome=lambda user, attempt:
                            FollowResult("excluded", "invalid_username"))
        run_follow(app)
        self.assertEqual(app.attempts, ["invalid/name"])
        self.assertEqual(app.takipler, [])
        self.assertEqual(storage.read_takiplist(), [])
        self.assertEqual(storage.read_takiplist(storage.KARALIST_PATH), ["invalid/name"])
        row = storage.get_user_statuses()[0]
        self.assertEqual((row["status"], row["reason"]), ("excluded", "invalid_username"))
        self.assertEqual(app.limiter.marked, 0)
        self.assertFalse(any("yeniden deneme" in reason.lower() for _, reason in app.waits))

    def test_existing_relationships_persist_without_counting_new_follows(self):
        results = {
            "new_follow": FollowResult("followed", "follow_confirmed", action_submitted=True),
            "new_request": FollowResult("requested", "request_confirmed", action_submitted=True),
            "existing_follow": FollowResult("already_following", "existing_relationship"),
            "existing_request": FollowResult("requested_existing", "existing_request"),
        }
        app = self.make_app(results, outcome=lambda user, attempt: results[user])
        run_follow(app)
        self.assertEqual(app.limiter.marked, 2)
        self.assertEqual(app.takipler, [])
        self.assertEqual(storage.read_takiplist(), [])
        self.assertEqual(set(storage.read_takiplist(storage.KARALIST_PATH)), set(results))
        rows = {row["username"]: row for row in storage.get_user_statuses()}
        for user, result in results.items():
            self.assertEqual((rows[user]["status"], rows[user]["reason"], rows[user]["attempts"]),
                             (result.status, result.reason, 1))
        with closing(sqlite3.connect(storage.STATUS_DB_PATH)) as connection:
            reasons = connection.execute(
                "SELECT reason FROM user_status_events WHERE username = ? ORDER BY id",
                ("new_follow",),
            ).fetchall()
        self.assertEqual(reasons[-1][0], "follow_confirmed")

    def test_stop_on_third_round_last_skip_reports_cancel_not_retry_cap(self):
        users = [f"user{i}" for i in range(5)]
        app = self.make_app(users)
        original_wait = app._wait_with_status

        def cancel_last_pause(seconds, reason):
            if "skip" in reason.lower() and len(app.attempts) == 15:
                app.stop_event.on_wait = app.stop_event.set
            return original_wait(seconds, reason)

        app._wait_with_status = cancel_last_pause
        run_follow(app)
        self.assertTrue(app.stop_event.is_set())
        self.assertEqual(Counter(app.attempts), Counter({user: 3 for user in users}))
        self.assertEqual(app.takipler, users)
        self.assertIn("iptal", app.logs[-1][1])
        self.assertFalse(any("3 takip turu tamamlandı" in message for _, message in app.logs))

    def test_existing_requests_do_not_reset_five_private_request_pause(self):
        users = [name for index in range(5) for name in (f"new{index}", f"existing{index}")]
        app = self.make_app(users, outcome=lambda user, attempt:
                            FollowResult("requested", "request_confirmed", action_submitted=True)
                            if user.startswith("new") else
                            FollowResult("requested_existing", "existing_request"))
        run_follow(app)
        self.assertEqual(app.limiter.marked, 5)
        self.assertEqual(app.attempts, users)
        self.assertEqual(app.takipler, [])
        self.assertTrue(any(seconds == 1800 and "private" in reason.lower()
                            for seconds, reason in app.waits))

    def test_collection_merge_preserves_newline_existing_reason_and_exclusions(self):
        app = self.make_app(["old_user"])
        collection = importlib.import_module(f"{PACKAGE_NAME}.collection")
        storage.TAKIPLIST_PATH.write_text("old_user", encoding="utf-8")
        storage.write_to_karalist("blocked_user")
        storage.record_user_status("old_user", "retryable_error", "profile_timeout")
        with patch.object(collection, "KARALIST_PATH", storage.KARALIST_PATH):
            added = app._save_collected_users(
                ["new_user", "blocked_user", "OLD_USER", "NEW_USER"], "Beğenilerden Topla")
        self.assertEqual(added, 1)
        self.assertEqual(app.takipler, ["old_user", "new_user"])
        self.assertEqual(storage.TAKIPLIST_PATH.read_text(encoding="utf-8"), "old_user\nnew_user\n")
        rows = {row["username"]: row for row in storage.get_user_statuses()}
        self.assertEqual(rows["old_user"]["reason"], "profile_timeout")
        self.assertEqual((rows["new_user"]["status"], rows["new_user"]["reason"],
                          rows["new_user"]["detail"], rows["new_user"]["attempts"]),
                         ("pending", "collected", "Beğenilerden Topla", 0))
        self.assertNotIn("blocked_user", rows)

    def test_empty_list_reports_completion_without_waiting(self):
        app = self.make_app()
        run_follow(app)
        self.assertEqual(app.attempts, [])
        self.assertEqual(app.waits, [])
        self.assertTrue(any("tamam" in message.lower() or "boş" in message.lower()
                            for _, message in app.logs))

    def test_daily_limit_pause_resumes_current_task_and_current_user(self):
        app = self.make_app(["first", "second"], outcome=lambda user, attempt: True)
        app.limiter.daily_blocked = True
        run_follow(app)
        self.assertEqual(app.attempts, ["first", "second"])
        self.assertEqual(app.takipler, [])
        self.assertEqual(app.limiter.daily_reset_calls, 1)
        app.trigger_daily_limit_button.assert_not_called()
        self.assertTrue(any(seconds >= 120 for seconds in app.safe_waits)
                        or any(seconds >= 120 for seconds, _ in app.waits))
        self.assertFalse(any(callback == app.trigger_daily_limit_button
                             for callback, _, _ in app.gui_queue.queue))

    def test_wait_reports_progress_and_returns_after_elapsed_duration(self):
        app = self.make_app()
        before = app.clock.now
        self.assertFalse(app._wait_with_status(130, "Test molası"))
        self.assertGreaterEqual(app.clock.now - before, 130)
        self.assertLess(app.clock.now - before, 132)
        self.assertGreaterEqual(len(app.logs), 3)
        gaps = [right[0] - left[0] for left, right in zip(app.logs, app.logs[1:])]
        self.assertTrue(all(gap <= 61 for gap in gaps), app.logs)

    def test_shutdown_cancels_wait_even_without_stop_event(self):
        app = self.make_app()
        app.shutdown_event.set()
        before = app.clock.now
        self.assertTrue(app._wait_with_status(180, "Test molası"))
        self.assertEqual(app.clock.now, before)

    def test_gui_callback_failure_does_not_block_following_updates(self):
        app = self.make_app()
        broken = Mock(side_effect=RuntimeError("widget unavailable"))
        healthy = Mock()
        app.call_in_main_thread(broken)
        app.call_in_main_thread(healthy, "next update")
        app.process_gui_queue()
        healthy.assert_called_once_with("next update")
        app.after.assert_called_once()
        self.assertEqual(app.after.call_args.args[1], app.process_gui_queue)
        self.assertTrue(any("widget unavailable" in message for _, message in app.logs))

    def test_dashboard_stop_during_pause_and_queued_task_does_not_hide_pause(self):
        app = self.make_app(["user"])
        app.active_task = "follow"
        app.dashboard.update(state="running", detail="Takip sürüyor")
        snapshots = []
        def during_wait():
            snapshots.append(app.dashboard.snapshot())
            app.topla_clicked()
            self.assertEqual(app.dashboard.snapshot()["state"], "waiting")
            app.stop_clicked()
        app.stop_event.on_wait = during_wait
        self.assertTrue(app._wait_with_status(180, "Skip molası"))
        self.assertEqual(snapshots[0]["wait_remaining"], 180)
        self.assertEqual(app.dashboard.snapshot()["state"], "stopping")
        app._dashboard_task_finished("follow")
        self.assertEqual(app.dashboard.snapshot()["state"], "stopped")
        self.assertIsNone(app.dashboard.snapshot()["wait_finish_at"])

    def test_dashboard_success_changes_only_for_confirmed_new_relationship(self):
        app = self.make_app(["confirmed", "existing", "retry", "request"])
        app._record_follow_result("confirmed", FollowResult("followed", "confirmed"))
        app._record_follow_result("existing", FollowResult("already_following", "existing"))
        app._record_follow_result("retry", FollowResult("retryable_error", "profile_timeout"))
        self.assertEqual(app.dashboard.snapshot()["last_success_user"], "confirmed")
        app._record_follow_result("request", FollowResult("requested", "confirmed"))
        self.assertEqual(app.dashboard.snapshot()["last_success_user"], "request")
        self.assertEqual(app.dashboard.snapshot()["remaining"], 1)

    def test_dashboard_empty_collection_inputs_require_attention(self):
        app = self.make_app()
        collection = importlib.import_module(f"{PACKAGE_NAME}.collection")
        with patch.object(collection, "read_all_post_links", return_value=[]):
            collection.CollectionTasks.collect_post_links(app)
        app._dashboard_task_finished("gonderiden_topla")
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")
        self.assertIn("gönderiler.txt", app.dashboard.snapshot()["detail"])

    def test_dashboard_partial_collection_not_marked_complete(self):
        app = self.make_app()
        collection = importlib.import_module(f"{PACKAGE_NAME}.collection")
        fake_page = SimpleNamespace(goto=Mock(side_effect=RuntimeError("unavailable")))
        with patch.object(collection, "read_all_post_links", return_value=["local-post-1", "local-post-2"]), \
                patch.object(collection, "KARALIST_PATH", app.directory / "missing.txt"), \
                patch.object(collection, "browser", SimpleNamespace(page=fake_page)):
            collection.CollectionTasks.collect_post_links(app)
        app._dashboard_task_finished("gonderiden_topla")
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")
        self.assertIn("2 uyarı", app.dashboard.snapshot()["detail"])

    def test_unexpected_result_processing_failure_keeps_worker_available(self):
        app = self.make_app(["user"], outcome=lambda user, attempt: True)
        app.namespace["write_to_karalist"].side_effect = OSError("simulated file error")
        run_follow(app, "login")
        self.assertEqual(app._ensure_bot_ready.call_count, 2,
                         "Worker must still process the next task after an exception")
        self.assertTrue(any("simulated file error" in message for _, message in app.logs))
        app.browser.close_bot.assert_called_once()

    def test_three_submitted_unconfirmed_results_stop_without_fourth_attempt(self):
        users = ["one", "two", "three", "four"]
        app = self.make_app(users, outcome=lambda user, attempt:
                            FollowResult("unconfirmed", "verification_timeout", "verification failed",
                                         action_submitted=True))
        run_follow(app, "login", "topla", "follow")
        self.assertEqual(app.attempts, users[:3])
        self.assertTrue(app.stop_event.is_set())
        self.assertTrue(app.follow_safety_stop)
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")
        self.assertEqual(app.dashboard.snapshot()["detail"], "Olası işlem kısıtlaması — hesabı kontrol et")
        self.assertEqual(app.takipler, users)
        self.assertEqual(storage.read_takiplist(storage.KARALIST_PATH), [])
        self.assertEqual(app.limiter.marked, 0)
        app._ensure_bot_ready.assert_called_once()
        app.collect_likes.assert_not_called()
        self.assertFalse(any(seconds == 180 and "skip" in reason.lower() for seconds, reason in app.waits))
        self.assertTrue(any(row["reason"] == "verification_timeout" for row in storage.get_user_statuses()))

    def test_two_unconfirmed_then_confirmed_new_resets_safety_counter(self):
        users = ["a", "b", "confirmed", "c", "d", "later"]
        app = self.make_app(users, outcome=lambda user, attempt:
                            FollowResult("unconfirmed", "verification_error", action_submitted=True)
                            if user in {"a", "b", "c", "d"} and attempt == 1 else
                            FollowResult("followed", "follow_confirmed", action_submitted=True))
        run_follow(app)
        self.assertFalse(app.follow_safety_stop)
        self.assertFalse(app.stop_event.is_set())
        self.assertIn("confirmed", app.attempts)
        self.assertGreaterEqual(app.attempts.count("later"), 1)

    def test_pre_click_timeout_keeps_retry_behavior_and_does_not_trigger_safety_stop(self):
        app = self.make_app(["user"], outcome=lambda user, attempt:
                            FollowResult("retryable_error", "profile_timeout", action_submitted=False))
        run_follow(app)
        self.assertEqual(app.attempts, ["user"] * 3)
        self.assertFalse(app.follow_safety_stop)
        self.assertFalse(app.stop_event.is_set())

    def test_existing_relationships_do_not_reset_submitted_unconfirmed_counter(self):
        results = {
            "u1": FollowResult("unconfirmed", "redirect", action_submitted=True),
            "existing": FollowResult("already_following", "existing_relationship"),
            "preclick": FollowResult("retryable_error", "profile_timeout"),
            "u2": FollowResult("unconfirmed", "timeout", action_submitted=True),
            "old_request": FollowResult("requested_existing", "profile_request_confirmed"),
            "u3": FollowResult("unconfirmed", "verification_error", action_submitted=True),
        }
        app = self.make_app(results, outcome=lambda user, attempt: results[user])
        run_follow(app)
        self.assertTrue(app.follow_safety_stop)
        self.assertEqual(app.attempts, list(results))

    def test_unconfirmed_counter_is_not_reset_by_retry_round(self):
        app = self.make_app(["first", "second"], outcome=lambda user, attempt:
                            FollowResult("unconfirmed", "verification_timeout", action_submitted=True))
        run_follow(app)
        self.assertEqual(app.attempts, ["first", "second", "first"])
        self.assertTrue(app.follow_safety_stop)
        self.assertEqual(storage.read_takiplist(), ["first", "second"])

    def test_cancelled_after_click_is_not_reported_as_safety_stop(self):
        def cancelled(user, attempt):
            app.stop_event.set()
            return FollowResult("cancelled", "cancelled", action_submitted=True)
        app = self.make_app(["user"], outcome=cancelled)
        run_follow(app)
        self.assertFalse(app.follow_safety_stop)
        self.assertTrue(app.stop_event.is_set())
        self.assertNotEqual(app.dashboard.snapshot()["detail"], "Olası işlem kısıtlaması — hesabı kontrol et")

    def test_follow_clicked_rearms_after_safety_stop_and_queues_follow(self):
        app = self.make_app(["user"])
        app.follow_safety_stop = True
        app.stop_event.set()
        app.follow_pending = False
        app.follow_clicked()
        self.assertFalse(app.stop_event.is_set())
        self.assertFalse(app.follow_safety_stop)
        self.assertEqual(list(app.task_queue.queue), ["follow"])

    def test_queued_tasks_are_skipped_while_follow_safety_stop_is_active(self):
        app = self.make_app(["user"])
        app.follow_safety_stop = True
        app.stop_event.set()
        app._ensure_bot_ready.reset_mock()
        app.collect_likes.reset_mock()
        app.collect_user_posts.reset_mock()
        app.collect_post_links.reset_mock()
        for task in ("login", "topla", "topla2", "gonderiden_topla", "follow", "shutdown"):
            app.task_queue.put(task)
        app.worker_loop()
        app._ensure_bot_ready.assert_not_called()
        app.collect_likes.assert_not_called()
        app.collect_user_posts.assert_not_called()
        app.collect_post_links.assert_not_called()
        self.assertTrue(app.follow_safety_stop)

    def test_non_follow_buttons_and_daily_limit_trigger_do_not_rearm_safety_stop(self):
        app = self.make_app(["user"])
        app.follow_safety_stop = True
        app.stop_event.set()
        app.follow_pending = False
        app.follow_btn = Mock()
        app.start_clicked()
        app.topla_clicked()
        worker.WorkerMixin.trigger_daily_limit_button(app)
        self.assertTrue(app.follow_safety_stop)
        self.assertTrue(app.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
