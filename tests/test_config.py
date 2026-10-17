"""Opening either tab must not migrate, prune, or reset the user's settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConfigReadOnlyTests(unittest.TestCase):
    def read_config(self, raw=None):
        with tempfile.TemporaryDirectory(prefix='instagram-config-test-') as folder:
            path = Path(folder) / 'config.json'
            if raw is not None:
                path.write_bytes(raw)
            command = ('import json; from instagram_bot.config import CONFIG; '
                       'print(json.dumps(CONFIG))')
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', command],
                                    cwd=Path(__file__).resolve().parents[1],
                                    env=dict(os.environ, INSTAGRAMTAKIP_DATA_DIR=folder),
                                    capture_output=True, text=True, timeout=10, check=True)
            return json.loads(result.stdout), path.read_bytes() if path.exists() else None

    def test_import_preserves_existing_bytes_and_unknown_fields(self):
        raw = b'{\n  "DAILY_LIMIT": 40, "HEADLESS_MODE": true, "custom_setting": "keep"\n}\n'
        config, after = self.read_config(raw)
        self.assertEqual(after, raw)
        self.assertEqual(config['DAILY_LIMIT'], 40)
        self.assertTrue(config['HEADLESS_MODE'])
        self.assertEqual(config['custom_setting'], 'keep')
        self.assertTrue(config['GONDERIDEN_EXCLUDE_FOLLOWING'])

    def test_default_settings_are_in_memory_until_explicit_save(self):
        config, after = self.read_config()
        self.assertEqual(config['DAILY_LIMIT'], 80)
        self.assertIsNone(after)

    def test_malformed_settings_are_not_silently_overwritten(self):
        raw = b'{ incomplete settings'
        config, after = self.read_config(raw)
        self.assertEqual(config['DAILY_LIMIT'], 80)
        self.assertEqual(after, raw)


if __name__ == '__main__':
    unittest.main()
