"""Exercise the real queue/store; only Instagram operations are replaced."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch


class UnfollowWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.export = self.folder / 'instagram-owner-2026-10-16-example'
        self.export.mkdir()
        (self.export / 'followers_1.json').write_text(json.dumps([
            {'string_list_data': [{'value': 'mutual', 'href': 'https://www.instagram.com/mutual/'}]}
        ]), encoding='utf-8')
        (self.export / 'following.json').write_text(json.dumps({'relationships_following': [
            {'title': name, 'string_list_data': [{'href': f'https://www.instagram.com/_u/{name}'}]}
            for name in ('mutual', 'alpha', 'beta')
        ]}), encoding='utf-8')
        self.events = []
        self.session_threads = []
        self.calls = []
        self.result = SimpleNamespace(status='unfollowed', detail='Doğrulandı.', action_submitted=True)
        self.identity = 'owner'
        self.block_started = threading.Event()
        self.release_action = threading.Event()
        self.block_action = False
        self.failure = None
        self.disk_pending_when_called = []
        self.external_pages = []
        self.emit_wait_restriction = False

    def make_controller(self):
        import instagram_bot.unfollow_worker as module
        outer = self

        class Page:
            def __init__(self):
                self.listeners = {}

            def on(self, event, callback):
                self.listeners.setdefault(event, []).append(callback)

            def remove_listener(self, event, callback):
                self.listeners[event].remove(callback)

            def wait_for_timeout(self, milliseconds):
                if outer.emit_wait_restriction:
                    outer.emit_wait_restriction = False
                    for callback in tuple(self.listeners.get('response', [])):
                        callback(SimpleNamespace(url='https://www.instagram.com/api/test/', status=429))
                threading.Event().wait(min(milliseconds / 1000, .01))

        class Session:
            page = None

            def __init__(self, profile_dir):
                self.profile_dir = profile_dir

            def open(self, stop_event=None):
                outer.session_threads.append(threading.get_ident())
                if self.page is None:
                    self.page = Page()
                return self.page

            def close(self):
                outer.session_threads.append(threading.get_ident())
                self.page = None

        def action(page, username, expected_account, stop_event=None):
            outer.external_pages.append(page)
            saved = json.loads((outer.folder / 'geri_takip_verileri.json').read_text(encoding='utf-8-sig'))
            active_account = saved['active_account']
            outer.disk_pending_when_called.append(saved['accounts'][active_account]['in_progress'])
            outer.calls.append((username, expected_account, threading.get_ident()))
            if outer.block_action:
                outer.block_started.set()
                if not outer.release_action.wait(5):
                    raise RuntimeError('Test action timed out')
            if outer.failure:
                raise outer.failure
            if stop_event.is_set():
                return SimpleNamespace(status='stopped', detail='Durduruldu.', action_submitted=False)
            return outer.result

        identity_patch = patch.object(module, 'session_username', side_effect=lambda page: self.identity)
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        controller = module.UnfollowController(self.folder, self.events.append,
                                              session_factory=Session, action=action)
        thread = threading.Thread(target=controller.run_loop, daemon=True)
        thread.start()

        def cleanup():
            self.release_action.set()
            controller.shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'Worker did not close')

        self.addCleanup(cleanup)
        self.controller, self.thread = controller, thread
        return controller

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while self.controller.busy and time.monotonic() < deadline:
            threading.Event().wait(.005)
        self.assertFalse(self.controller.busy, 'Worker never became idle')

    def load(self):
        self.assertTrue(self.controller.import_paths([self.export]))
        self.wait_idle()
        self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])

    def test_import_does_not_open_instagram_and_preserves_invalid_import(self):
        self.make_controller()
        self.load()
        self.assertEqual(self.session_threads, [])
        self.assertTrue(self.controller.import_paths([self.folder / 'absent.zip']))
        self.wait_idle()
        self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])
        self.assertTrue(any(e.get('state') == 'attention' for e in self.events))

    def test_one_verified_action_decrements_once_and_survives_restart(self):
        self.make_controller()
        self.load()
        self.assertTrue(self.controller.unfollow('alpha'))
        self.wait_idle()
        saved = self.controller.snapshot()
        self.assertEqual(saved['remaining'], ['beta'])
        self.assertEqual(saved['history'][0]['status'], 'unfollowed')
        self.assertEqual([c[:2] for c in self.calls], [('alpha', 'owner')])
        self.assertEqual(self.disk_pending_when_called, ['alpha'])
        self.assertTrue(all(t == self.thread.ident for t in self.session_threads))
        from instagram_bot.unfollow_store import UnfollowStore
        restored = UnfollowStore(self.folder / 'geri_takip_verileri.json').snapshot()
        self.assertEqual(restored['remaining'], ['beta'])

    def test_failed_or_unknown_action_never_decrements(self):
        self.make_controller()
        self.load()
        for status in ('error', 'unconfirmed', 'restricted', 'stopped', 'unexpected'):
            with self.subTest(status=status):
                self.result = SimpleNamespace(status=status, detail='Sonuç belirsiz.', action_submitted=True)
                self.assertTrue(self.controller.unfollow('alpha'))
                self.wait_idle()
                self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])
                self.assertNotIn(self.controller.snapshot()['history'][0]['status'],
                                 ('unfollowed', 'already_not_following'))

    def test_wrong_signed_in_account_never_calls_action(self):
        self.make_controller()
        self.load()
        self.identity = 'someone_else'
        self.assertTrue(self.controller.unfollow('alpha'))
        self.wait_idle()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])

    def test_state_write_failure_prevents_external_action(self):
        self.make_controller()
        self.load()
        with patch.object(self.controller._store, 'begin', side_effect=OSError('Disk write failed')):
            self.assertTrue(self.controller.unfollow('alpha'))
            self.wait_idle()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])

    def test_corrupt_saved_state_is_not_silently_replaced_by_import(self):
        path = self.folder / 'geri_takip_verileri.json'
        path.write_text('invalid saved state', encoding='utf-8')
        self.make_controller()
        self.assertTrue(self.controller.import_paths([self.export]))
        self.wait_idle()
        self.assertEqual(path.read_text(encoding='utf-8'), 'invalid saved state')
        self.assertEqual(self.calls, [])

    def test_no_queued_batch_or_duplicate_action_while_busy(self):
        self.make_controller()
        self.load()
        self.block_action = True
        self.assertTrue(self.controller.unfollow('alpha'))
        self.assertTrue(self.block_started.wait(5))
        self.assertFalse(self.controller.unfollow('alpha'))
        self.assertFalse(self.controller.unfollow('beta'))
        self.assertFalse(self.controller.import_paths([self.export]))
        self.release_action.set()
        self.wait_idle()
        self.assertEqual(self.controller.snapshot()['remaining'], ['beta'])
        self.assertEqual(len(self.calls), 1)

    def test_stop_during_action_keeps_person_and_never_processes_next(self):
        self.make_controller()
        self.load()
        self.block_action = True
        self.assertTrue(self.controller.unfollow('alpha'))
        self.assertTrue(self.block_started.wait(5))
        self.controller.stop()
        self.release_action.set()
        self.wait_idle()
        self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.controller.snapshot()['history'][0]['status'], 'stopped')

    def test_action_exception_is_recorded_without_losing_candidate(self):
        self.make_controller()
        self.load()
        self.failure = RuntimeError('Disconnected after possible click')
        self.assertTrue(self.controller.unfollow('alpha'))
        self.wait_idle()
        state = self.controller.snapshot()
        self.assertEqual(state['remaining'], ['alpha', 'beta'])
        self.assertEqual(state['history'][0]['status'], 'unconfirmed')
        self.assertFalse(state['in_progress'])

    def test_arbitrary_username_cannot_be_unfollowed(self):
        self.make_controller()
        self.load()
        self.assertTrue(self.controller.unfollow('not_in_export'))
        self.wait_idle()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.session_threads, [])

    def test_follow_runs_on_shared_worker_and_reuses_unfollow_session(self):
        self.make_controller()
        self.load()
        observed = []

        def follow(page, stop_event, shutdown_event):
            observed.append((page, threading.get_ident(), stop_event.is_set(), shutdown_event.is_set()))
            return {'state': 'idle', 'detail': 'Takip görevi tamamlandı.'}

        self.assertTrue(self.controller.follow(follow))
        self.wait_idle()
        self.assertEqual(observed[0][1:], (self.thread.ident, False, False))
        self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])
        self.assertTrue(self.controller.unfollow('alpha'))
        self.wait_idle()
        self.assertIs(observed[0][0], self.external_pages[0])

    def test_follow_locks_out_second_browser_operation(self):
        self.make_controller()
        self.load()

        def follow(page, stop_event, shutdown_event):
            self.block_started.set()
            self.release_action.wait(5)
            return {'state': 'idle', 'detail': 'Bitti.'}

        self.assertTrue(self.controller.follow(follow))
        self.assertTrue(self.block_started.wait(5))
        self.assertFalse(self.controller.unfollow('alpha'))
        self.assertFalse(self.controller.follow(follow))
        self.assertFalse(self.controller.import_paths([self.export]))
        self.release_action.set()
        self.wait_idle()
        self.assertEqual(self.calls, [])

    def test_follow_stop_uses_shared_cancellation_event(self):
        self.make_controller()

        def follow(page, stop_event, shutdown_event):
            self.block_started.set()
            self.release_action.wait(5)
            self.assertTrue(stop_event.is_set())
            return {'state': 'stopped', 'detail': 'Takip durduruldu.'}

        self.assertTrue(self.controller.follow(follow))
        self.assertTrue(self.block_started.wait(5))
        self.controller.stop()
        self.release_action.set()
        self.wait_idle()
        self.assertTrue(any(e.get('state') == 'stopped' and e.get('detail') == 'Takip durduruldu.' for e in self.events))

    def test_follow_wrong_account_never_calls_legacy_callback(self):
        self.make_controller()
        self.load()
        self.identity = 'other'
        observed = []
        self.assertTrue(self.controller.follow(lambda *args: observed.append(args)))
        self.wait_idle()
        self.assertEqual(observed, [])

    def test_follow_restriction_overrides_optimistic_completion(self):
        self.make_controller()

        def follow(page, stop_event, shutdown_event):
            for listener in tuple(page.listeners.get('response', [])):
                listener(SimpleNamespace(url='https://www.instagram.com/api/test/', status=429))
            self.assertTrue(stop_event.is_set())
            return {'state': 'idle', 'detail': 'Bitti.'}

        self.assertTrue(self.controller.follow(follow))
        self.wait_idle()
        self.assertTrue(any(e.get('state') == 'attention' and '429' in e.get('detail', '') for e in self.events))

    def test_batch_processes_remaining_in_order_and_persists_each_result(self):
        self.make_controller()
        self.load()
        with patch('instagram_bot.unfollow_worker.UNFOLLOW_INTERVAL_SECONDS', 0):
            self.assertTrue(self.controller.unfollow_batch())
            self.wait_idle()
        self.assertEqual([c[0] for c in self.calls], ['alpha', 'beta'])
        self.assertEqual(self.disk_pending_when_called, ['alpha', 'beta'])
        self.assertEqual(self.controller.snapshot()['remaining'], [])
        self.assertEqual(len(self.controller.snapshot()['history']), 2)

    def test_batch_removes_unavailable_person_and_continues_without_false_success(self):
        self.make_controller()
        self.load()
        original = self.controller._action

        def per_person(page, username, *args, **kwargs):
            self.result = SimpleNamespace(status='unavailable' if username == 'alpha' else 'unfollowed',
                                          detail='Profile ulaşılamadı.' if username == 'alpha' else 'Doğrulandı.',
                                          action_submitted=username != 'alpha')
            return original(page, username, *args, **kwargs)

        self.controller._action = per_person
        with patch('instagram_bot.unfollow_worker.UNFOLLOW_INTERVAL_SECONDS', 0):
            self.assertTrue(self.controller.unfollow_batch())
            self.wait_idle()
        self.assertEqual([c[0] for c in self.calls], ['alpha', 'beta'])
        state = self.controller.snapshot()
        self.assertEqual(state['remaining'], [])
        self.assertEqual(state['last_results']['alpha']['status'], 'unavailable')
        self.assertFalse(state['last_results']['alpha']['submitted'])
        self.assertTrue(any('Doğrulanan: 1' in e.get('detail', '') and 'Ulaşılamayan: 1' in e.get('detail', '') for e in self.events))
        self.assertTrue(state['last_results']['alpha']['target_verified'])

    @patch('instagram_bot.unfollow_worker.UNFOLLOW_BATCH_SIZE', 1)
    def test_batch_really_advances_after_a_nonzero_pause(self):
        self.make_controller()
        self.load()
        with patch('instagram_bot.unfollow_worker.UNFOLLOW_INTERVAL_SECONDS', .05):
            self.assertTrue(self.controller.unfollow_batch())
            self.wait_idle()
        self.assertEqual([c[0] for c in self.calls], ['alpha', 'beta'])
        self.assertEqual(self.controller.snapshot()['remaining'], [])
        self.assertTrue(any(e.get('phase') == 'waiting' for e in self.events))

    def test_batch_stops_on_every_unverified_result_without_next_person(self):
        self.make_controller()
        self.load()
        for status in ('restricted', 'unconfirmed', 'error', 'stopped'):
            self.result = SimpleNamespace(status=status, detail='Kontrol gerekli.', action_submitted=True)
            before = len(self.calls)
            with patch('instagram_bot.unfollow_worker.UNFOLLOW_INTERVAL_SECONDS', 0):
                self.assertTrue(self.controller.unfollow_batch())
                self.wait_idle()
            self.assertEqual(len(self.calls), before + 1)
            self.assertEqual(self.calls[-1][0], 'alpha')
            self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])

    @patch('instagram_bot.unfollow_worker.UNFOLLOW_BATCH_SIZE', 1)
    def test_stop_interrupts_batch_wait_and_blocks_other_jobs(self):
        self.make_controller()
        self.load()
        self.assertTrue(self.controller.unfollow_batch())
        deadline = time.monotonic() + 5
        while len(self.calls) < 1 and time.monotonic() < deadline:
            threading.Event().wait(.005)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.controller.unfollow('beta'))
        self.assertFalse(self.controller.follow(lambda *args: None))
        self.assertFalse(self.controller.import_paths([self.export]))
        self.controller.stop()
        self.wait_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.controller.snapshot()['remaining'], ['beta'])

    def test_batch_wrong_account_or_empty_list_never_calls_action(self):
        self.make_controller()
        self.assertTrue(self.controller.unfollow_batch())
        self.wait_idle()
        self.assertEqual(self.session_threads, [])
        self.load()
        self.identity = 'someone_else'
        self.assertTrue(self.controller.unfollow_batch())
        self.wait_idle()
        self.assertEqual(self.calls, [])

    def test_batch_does_not_resume_on_ordinary_startup(self):
        self.make_controller()
        self.load()
        self.controller.shutdown()
        self.thread.join(5)
        self.make_controller()
        self.assertEqual(self.controller.snapshot()['remaining'], ['alpha', 'beta'])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.controller.busy)

    @patch('instagram_bot.unfollow_worker.UNFOLLOW_BATCH_SIZE', 1)
    def test_batch_detects_restriction_while_waiting_and_never_starts_next(self):
        self.make_controller()
        self.load()
        self.emit_wait_restriction = True
        self.assertTrue(self.controller.unfollow_batch())
        self.wait_idle()
        self.assertEqual([c[0] for c in self.calls], ['alpha'])
        self.assertEqual(self.controller.snapshot()['remaining'], ['beta'])
        self.assertTrue(any(e.get('state') == 'attention' and '429' in e.get('detail', '') for e in self.events))
        self.assertEqual(self.external_pages[0].listeners.get('response'), [])

    @patch('instagram_bot.unfollow_worker.UNFOLLOW_BATCH_SIZE', 1)
    def test_batch_waits_for_previous_operation_after_restart(self):
        self.make_controller()
        self.load()
        self.assertTrue(self.controller.unfollow('alpha'))
        self.wait_idle()
        self.controller.shutdown()
        self.thread.join(5)
        self.make_controller()
        self.assertTrue(self.controller.unfollow_batch())
        threading.Event().wait(.1)
        self.assertTrue(self.controller.busy)
        self.assertEqual([c[0] for c in self.calls], ['alpha'])
        self.controller.stop()
        self.wait_idle()
        self.assertEqual(self.controller.snapshot()['remaining'], ['beta'])

    def test_batch_rechecks_account_before_every_person(self):
        self.make_controller()
        self.load()
        original_action = self.controller._action

        def switch_account(*args, **kwargs):
            result = original_action(*args, **kwargs)
            self.identity = 'other_account'
            return result

        self.controller._action = switch_account
        with patch('instagram_bot.unfollow_worker.UNFOLLOW_INTERVAL_SECONDS', 0):
            self.assertTrue(self.controller.unfollow_batch())
            self.wait_idle()
        self.assertEqual([c[0] for c in self.calls], ['alpha'])
        self.assertEqual(self.controller.snapshot()['remaining'], ['beta'])

    def test_batch_reports_wrong_account_before_entering_a_long_wait(self):
        self.make_controller()
        self.load()
        self.assertTrue(self.controller.unfollow('alpha'))
        self.wait_idle()
        self.identity = 'other_account'
        with patch.object(self.controller, '_wait_between_unfollows', return_value=False) as wait:
            self.assertTrue(self.controller.unfollow_batch())
            self.wait_idle()
        wait.assert_not_called()
        self.assertEqual([c[0] for c in self.calls], ['alpha'])

    def test_batch_finish_write_failure_prevents_next_action(self):
        self.make_controller()
        self.load()
        with patch.object(self.controller._store, 'finish', side_effect=OSError('Cannot save result')):
            self.assertTrue(self.controller.unfollow_batch())
            self.wait_idle()
        self.assertEqual([c[0] for c in self.calls], ['alpha'])
        state = self.controller.snapshot()
        self.assertEqual(state['in_progress'], 'alpha')
        self.assertEqual(state['remaining'], ['alpha', 'beta'])
        self.assertEqual(state['history'], [])


    def test_five_successes_then_pause_without_daily_cap(self):
        self.make_controller()
        self.load()
        now = datetime.now(timezone.utc)
        for count, expected_waits in ((4, 0), (5, 1), (6, 0), (20, 1), (21, 0)):
            with patch.object(self.controller, '_batch_history', return_value=(now, [now] * count)), \
                    patch.object(self.controller, '_wait_between_unfollows', return_value=False) as wait:
                # Only inspect the first candidate; no external operation is needed.
                with patch.object(self.controller, '_unfollow', return_value='error') as action:
                    self.assertTrue(self.controller.unfollow_batch())
                    self.wait_idle()
                    self.assertEqual(wait.call_count, expected_waits)
                    self.assertEqual(action.call_count, 1 - expected_waits)
                    if expected_waits:
                        self.assertGreaterEqual(wait.call_args.args[0], 600)



if __name__ == '__main__':
    unittest.main()
