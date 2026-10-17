"""Offline regressions for manual scan scheduling and persistent pacing."""
import importlib
import importlib.util
import os
from pathlib import Path
import queue
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_offline_scan_worker_fixture"


def setUpModule():
    global folder, worker, dashboard
    folder = TemporaryDirectory(prefix="instagram-scan-worker-import-")
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "instagram_bot" / "__init__.py",
        submodule_search_locations=[str(ROOT / "instagram_bot")],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    with patch.dict(os.environ, {"INSTAGRAMTAKIP_DATA_DIR": folder.name}):
        spec.loader.exec_module(package)
        worker = importlib.import_module(f"{PACKAGE}.worker")
        dashboard = importlib.import_module(f"{PACKAGE}.dashboard")


def tearDownModule():
    logger = importlib.import_module(f"{PACKAGE}.logging_setup").APP_LOG
    for handler in list(logger.handlers):
        if Path(getattr(handler, "baseFilename", ".")).resolve().parent == Path(folder.name).resolve():
            handler.close()
            logger.removeHandler(handler)
    for name in list(sys.modules):
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            del sys.modules[name]
    folder.cleanup()


class ScanWorkerTests(unittest.TestCase):
    def setUp(self):
        self.data = TemporaryDirectory(prefix="instagram-scan-worker-")
        self.addCleanup(self.data.cleanup)
        self.directory = Path(self.data.name)
        self.now = 10000.0
        patcher = patch.multiple(
            worker, BASE_DIR=self.directory,
            browser=SimpleNamespace(page=object(), rate_limit_lock_until=0, close_bot=Mock()), create=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = patch.object(worker.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def make_app(self):
        app = worker.WorkerMixin()
        app.task_queue = queue.Queue()
        app.stop_event = threading.Event()
        app.shutdown_event = threading.Event()
        app.active_task = None
        app.follow_pending = False
        app.follow_safety_stop = False
        app.non_followers_pending = False
        app.non_followers_result = None
        app.takipler = ["existing_candidate"]
        app.dashboard = dashboard.DashboardState(remaining=1)
        app.logs = []
        app.results = []
        app.log = app.logs.append
        app._ensure_bot_ready = Mock(return_value=True)
        app.call_in_main_thread = lambda callback, *args: callback(*args)
        app._show_non_followers_result = app.results.append
        app._refresh_non_followers_window = lambda: None
        app.stats = SimpleNamespace(record_error=Mock())
        app.update_stats_panel = lambda: None
        return app

    def scanner(self, status="completed"):
        self.scans = []
        result = SimpleNamespace(status=status, detail="fixture", username="owner",
                                 followers=("friend",), following=("friend", "other"),
                                 not_following_back=("other",) if status == "completed" else ())

        def scan(page, stop_event, log_cb=None, progress_cb=None):
            self.scans.append(page)
            return result

        def result_factory(status, **kwargs):
            defaults = dict(username="", followers=(), following=(), not_following_back=(), detail="", checked_at="")
            defaults.update(kwargs)
            return SimpleNamespace(status=status, **defaults)

        patcher = patch.dict(sys.modules, {f"{PACKAGE}.non_followers": SimpleNamespace(
            scan_non_followers=scan, NonFollowersResult=result_factory)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_busy_scan_does_not_queue_or_resume_another_task(self):
        app = self.make_app()
        app.active_task = "follow"
        app.stop_event.set()
        app.non_followers_clicked()
        self.assertTrue(app.task_queue.empty())
        self.assertTrue(app.stop_event.is_set())
        self.assertFalse(app.non_followers_pending)

    def test_duplicate_scan_and_other_actions_do_not_resume_stopped_scan(self):
        app = self.make_app()
        app.non_followers_clicked()
        app.stop_event.set()
        app.non_followers_clicked()
        for action in (app.start_clicked, app.follow_clicked, app.topla_clicked,
                       app.topla2_clicked, app.gonderiden_topla_clicked):
            action()
        self.assertEqual(app.task_queue.qsize(), 1)
        self.assertEqual(app.task_queue.get_nowait(), "non_followers")
        self.assertTrue(app.stop_event.is_set())

    @patch("_offline_scan_worker_fixture.worker.NON_FOLLOWERS_COOLDOWN", 1800)
    def test_attempt_persists_and_new_app_cannot_scan_again_immediately(self):
        app = self.make_app()
        expected = self.scanner()
        app._run_non_followers_task()
        self.assertEqual(app.results, [expected])
        restarted = self.make_app()
        restarted.non_followers_clicked()
        self.assertTrue(restarted.task_queue.empty())
        self.assertEqual(len(self.scans), 1)
        self.now += 1801
        restarted.non_followers_clicked()
        self.assertEqual(restarted.task_queue.get_nowait(), "non_followers")

    @patch("_offline_scan_worker_fixture.worker.NON_FOLLOWERS_COOLDOWN", 1800)
    def test_corrupt_history_blocks_scan_before_browser_access(self):
        (self.directory / "geri_takip_tarama.json").write_text("broken", encoding="utf-8")
        app = self.make_app()
        app.non_followers_clicked()
        self.assertTrue(app.task_queue.empty())
        self.assertFalse(app.non_followers_pending)
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")
        self.assertFalse(app._ensure_bot_ready.called)

    def test_stop_before_start_does_not_log_in_or_consume_cooldown(self):
        app = self.make_app()
        self.scanner()
        app.stop_event.set()
        app._run_non_followers_task()
        self.assertFalse(app._ensure_bot_ready.called)
        self.assertEqual(self.scans, [])
        self.assertFalse((self.directory / "geri_takip_tarama.json").exists())
        self.assertEqual(app.results[-1].status, "stopped")

    @patch("_offline_scan_worker_fixture.worker.NON_FOLLOWERS_COOLDOWN", 1800)
    def test_login_failure_is_shown_in_result_window(self):
        app = self.make_app()
        self.scanner()
        app._ensure_bot_ready = lambda: False
        app._run_non_followers_task()
        self.assertEqual(self.scans, [])
        self.assertEqual(app.results[-1].status, "error")
        self.assertTrue(app.results[-1].detail)
        self.assertTrue((self.directory / "geri_takip_tarama.json").exists())

    def test_manual_read_only_recheck_after_restriction_never_starts_following(self):
        app = self.make_app()
        self.scanner("restricted")
        app._run_non_followers_task()
        app.non_followers_clicked()
        self.assertEqual(app.task_queue.get_nowait(), "non_followers")
        self.assertTrue(app.follow_safety_stop, "Read recheck must not re-enable follow automation")

    def test_restriction_stops_queued_follow_and_clears_pending(self):
        app = self.make_app()
        expected = self.scanner("restricted")
        app.non_followers_pending = True
        app.collect_likes = lambda: None
        app.collect_user_posts = lambda: None
        app.collect_post_links = lambda: None
        follow_attempts = []
        app._run_follow_task = lambda: follow_attempts.append(True)
        for task in ("non_followers", "follow", "shutdown"):
            app.task_queue.put(task)
        app.worker_loop()
        self.assertEqual(app.results, [expected])
        self.assertEqual(follow_attempts, [])
        self.assertTrue(app.follow_safety_stop)
        self.assertTrue(app.stop_event.is_set())
        self.assertFalse(app.non_followers_pending)
        self.assertIsNone(app.active_task)
        self.assertEqual(app.task_queue.unfinished_tasks, 0)
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")

    @patch("_offline_scan_worker_fixture.worker.NON_FOLLOWERS_COOLDOWN", 1800)
    def test_worker_rechecks_cooldown_and_does_not_repeat_scan(self):
        app = self.make_app()
        self.scanner()
        app._run_non_followers_task()
        app._run_non_followers_task()
        self.assertEqual(len(self.scans), 1)

    def test_disabled_cooldown_allows_immediate_retry_despite_existing_history(self):
        expected = self.scanner("incomplete")
        history = self.directory / "geri_takip_tarama.json"
        for content in ('{"last_attempt_at": 10000}', '{"last_attempt_at": 20000}', "broken"):
            with self.subTest(history=content):
                history.write_text(content, encoding="utf-8")
                app = self.make_app()
                app.non_followers_clicked()
                self.assertEqual(app.task_queue.qsize(), 1)
                self.assertEqual(app.task_queue.get_nowait(), "non_followers")
                app._run_non_followers_task()
                app._run_non_followers_task()
                self.assertEqual(app.results, [expected, expected])
                self.assertEqual(app._ensure_bot_ready.call_count, 2)
                self.assertEqual(history.read_text(encoding="utf-8"), content)

    def test_disabled_cooldown_keeps_instagram_rate_limit_lock(self):
        worker.browser.rate_limit_lock_until = self.now + 60
        app = self.make_app()
        app.non_followers_clicked()
        self.assertTrue(app.task_queue.empty())
        self.assertFalse(app._ensure_bot_ready.called)
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")

    def test_incomplete_scan_stays_attention_without_mutating_follow_candidates(self):
        app = self.make_app()
        expected = self.scanner("incomplete")
        app._run_non_followers_task()
        self.assertEqual(app.results, [expected])
        self.assertEqual(app.takipler, ["existing_candidate"])
        self.assertEqual(app.dashboard.snapshot()["state"], "attention")


if __name__ == "__main__":
    unittest.main()
