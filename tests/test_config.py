import json
from pathlib import Path
import tempfile
import unittest

from src.config import Settings


class ConfigTests(unittest.TestCase):
    def test_whitelist_expiration_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "discord": {
                    "token": "token",
                    "allowed_user_ids": [222],
                    "allowed_user_expirations": {"222": 2000000000},
                },
                "woolix": {"api_key": "key"},
                "database": {"url": "postgresql://example/db"},
                "tracking": {"base_url": "https://relay.example/", "slug_secret": "secret"},
            }), encoding="utf-8")

            settings = Settings.from_file(path)
            self.assertEqual(settings.allowed_user_expirations, {222: 2000000000})
            self.assertEqual(settings.database_url, "postgresql://example/db")
            self.assertEqual(settings.tracker_base_url, "https://relay.example/")
            self.assertEqual(settings.tracker_slug_secret, "secret")
            settings.allowed_user_ids.add(333)
            settings.allowed_user_expirations[333] = 2100000000
            settings.save_runtime()

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["discord"]["allowed_user_expirations"],
                {"222": 2000000000, "333": 2100000000},
            )


if __name__ == "__main__":
    unittest.main()
