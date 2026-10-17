"""Two launches must never independently overwrite one account's history."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_second_guard_is_rejected_until_first_closes(self):
        from instagram_bot.single_instance import SingleInstance
        first = SingleInstance(self.folder)
        second = SingleInstance(self.folder)
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())

    def test_different_data_folders_are_independent(self):
        from instagram_bot.single_instance import SingleInstance
        first = SingleInstance(self.folder / 'one')
        second = SingleInstance(self.folder / 'two')
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        self.assertTrue(first.acquire())
        self.assertTrue(second.acquire())

    def test_another_process_cannot_acquire_live_app_data(self):
        from instagram_bot.single_instance import SingleInstance
        guard = SingleInstance(self.folder)
        self.addCleanup(guard.release)
        code = ('import sys; from instagram_bot.single_instance import SingleInstance; '
                'guard=SingleInstance(sys.argv[1]); print(guard.acquire()); guard.release()')

        def child():
            result = subprocess.run([sys.executable, '-c', code, str(self.folder)],
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=10, check=True)
            return result.stdout.strip()

        self.assertTrue(guard.acquire())
        self.assertEqual(child(), 'False')
        guard.release()
        self.assertEqual(child(), 'True')


if __name__ == '__main__':
    unittest.main()
