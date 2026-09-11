import unittest

from src.user_settings import UserSettingsStore, mask_secret


class UserSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = UserSettingsStore("", "test-encryption-secret")

    def test_getatext_key_round_trip_is_encrypted(self):
        key = "1234567890abcdef1234567890"

        self.store.set_getatext_key(123, key)

        nonce, ciphertext = self.store._memory[123]
        self.assertNotIn(key.encode(), nonce + ciphertext)
        self.assertEqual(self.store.get_getatext_key(123), key)

    def test_getatext_key_can_be_removed(self):
        self.store.set_getatext_key(123, "1234567890abcdef")
        self.assertTrue(self.store.clear_getatext_key(123))
        self.assertIsNone(self.store.get_getatext_key(123))
        self.assertFalse(self.store.clear_getatext_key(123))

    def test_getatext_key_length_is_validated(self):
        with self.assertRaises(ValueError):
            self.store.set_getatext_key(123, "short")

    def test_secret_mask_never_displays_the_full_key(self):
        key = "1234567890abcdef1234567890"
        masked = mask_secret(key)
        self.assertEqual(masked, "1234••••••7890")
        self.assertNotIn(key, masked)
        self.assertEqual(mask_secret(None), "Not connected")
