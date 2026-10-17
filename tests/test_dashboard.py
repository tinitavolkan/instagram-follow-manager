"""Pure dashboard state tests; no Tkinter, browser, credentials or files."""
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import threading
import unittest

spec = importlib.util.spec_from_file_location(
    "_dashboard_unit", Path(__file__).resolve().parents[1] / "instagram_bot" / "dashboard.py")
dashboard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard_module)
DashboardState = dashboard_module.DashboardState


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0
        self.start = datetime(2026, 9, 5, 23, 59, 0)
        self.state = DashboardState(3, monotonic=lambda: self.elapsed,
                                    now=lambda: self.start + timedelta(seconds=self.elapsed))

    def test_countdown_does_not_claim_resume_without_worker(self):
        token = self.state.begin_wait(180, "Test molası")
        self.elapsed = 61
        self.assertEqual(self.state.snapshot()["wait_remaining"], 119)
        self.assertEqual(self.state.snapshot()["wait_finish_at"], datetime(2026, 9, 6, 0, 2))
        self.elapsed = 300
        self.assertEqual(self.state.snapshot()["wait_remaining"], 0)
        self.assertEqual(self.state.snapshot()["state"], "waiting")
        self.state.end_wait(token)
        self.assertEqual(self.state.snapshot()["state"], "ready")

    def test_nested_wait_restores_outer_then_original_state(self):
        self.state.update(state="running", detail="Profil kontrol ediliyor")
        outer = self.state.begin_wait(300, "Dış mola")
        inner = self.state.begin_wait(30, "İç mola")
        self.state.end_wait(inner)
        self.assertEqual(self.state.snapshot()["detail"], "Dış mola")
        self.state.end_wait(outer)
        self.assertEqual(self.state.snapshot()["detail"], "Profil kontrol ediliyor")
        self.assertIsNone(self.state.snapshot()["wait_remaining"])

    def test_late_wait_end_cannot_overwrite_stop_or_attention(self):
        token = self.state.begin_wait(180, "Mola")
        self.state.update(state="stopping", detail="Kullanıcı durdurdu")
        self.state.end_wait(token)
        self.assertEqual(self.state.snapshot()["state"], "stopping")
        self.assertIsNone(self.state.begin_wait(180, "Geç bildirim"))
        self.state.update(state="attention", detail="Oturum açılamadı")
        self.state.end_wait(token, stopped=True)
        self.assertEqual(self.state.snapshot()["detail"], "Oturum açılamadı")

    def test_remaining_refresh_does_not_cancel_wait_or_success(self):
        self.state.mark_success("confirmed_user")
        self.state.begin_wait(180, "Mola")
        self.state.update(remaining=2)
        current = self.state.snapshot()
        self.assertEqual((current["state"], current["remaining"]), ("waiting", 2))
        self.assertEqual(current["last_success_user"], "confirmed_user")
        self.assertEqual(current["last_success_at"], self.start)

    def test_reported_wait_restores_state_on_exception(self):
        self.state.update(state="running")
        with self.assertRaisesRegex(RuntimeError, "example"):
            with dashboard_module.reporting_scope(self.state):
                with dashboard_module.reported_wait(60):
                    self.assertEqual(self.state.snapshot()["state"], "waiting")
                    raise RuntimeError("example")
        self.assertEqual(self.state.snapshot()["state"], "running")
        self.assertIsNone(self.state.snapshot()["wait_remaining"])

    def test_reporting_is_thread_local_and_stop_is_retained(self):
        errors = []
        stop = threading.Event()
        with dashboard_module.reporting_scope(self.state):
            def unrelated_worker():
                try:
                    with dashboard_module.reported_wait(999):
                        self.assertEqual(self.state.snapshot()["state"], "ready")
                except Exception as error:
                    errors.append(error)
            other = threading.Thread(target=unrelated_worker)
            other.start()
            other.join(timeout=2)
            self.assertFalse(other.is_alive())
            self.assertEqual(errors, [])
            with dashboard_module.wait_description("Saatlik limit"):
                with dashboard_module.reported_wait(60, stop_event=stop):
                    self.assertEqual(self.state.snapshot()["detail"], "Saatlik limit")
                    stop.set()
        self.assertEqual(self.state.snapshot()["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
