"""Release defaults must not touch credentials or the source checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_data_directory_is_separate_and_can_be_overridden(self):
        for custom in (False, True):
            with self.subTest(custom=custom), tempfile.TemporaryDirectory() as folder:
                env = dict(os.environ)
                env.pop('INSTAGRAMTAKIP_DATA_DIR', None)
                env.update(LOCALAPPDATA=folder, XDG_DATA_HOME=folder)
                expected = Path(folder) / 'InstagramFollowManager'
                if custom:
                    expected = Path(folder) / 'chosen'
                    env['INSTAGRAMTAKIP_DATA_DIR'] = str(expected)
                result = subprocess.run(
                    [sys.executable, '-c', 'from instagram_bot.paths import BASE_DIR; print(BASE_DIR)'],
                    cwd=ROOT, env=env, capture_output=True, text=True, check=True)
                self.assertEqual(Path(result.stdout.strip()), expected.resolve())
                self.assertFalse(expected.exists(), 'Import must not create runtime files')

    def test_legacy_browser_entry_cannot_read_credentials_or_start_a_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, INSTAGRAMTAKIP_DATA_DIR=folder)
            code = '''
from unittest.mock import patch
from pathlib import Path
from instagram_bot import browser
with patch.object(Path, 'read_text', side_effect=AssertionError('No reads')):
    try:
        browser.run_bot()
    except RuntimeError as error:
        assert 'elle giriş' in str(error)
    else:
        raise AssertionError('Legacy password login was allowed')
'''
            subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=ROOT, env=env, check=True)

    def test_demo_import_is_synthetic_and_has_expected_difference(self):
        from instagram_bot.export_lists import load_exports
        result = load_exports([ROOT / 'examples' / 'demo-export'])
        self.assertEqual(set(result.following) - set(result.followers), {'demo_candidate'})


if __name__ == '__main__':
    unittest.main()
