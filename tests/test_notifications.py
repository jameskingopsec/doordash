import unittest
import base64
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.models import OrderSession
from src.notifications import G6_SUCCESS, success_webhook_payload, webhook_url_with_wait
from src.tracking import SLUG_AAD, configure_tracking, order_tracker_url


TEST_SLUG_SECRET = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()


class NotificationTests(unittest.TestCase):
    def test_success_payload_is_compact_and_public_safe(self):
        session = OrderSession(user_id=123, fulfillment="delivery", card_last4="1111")
        job = {
            "order_uuid": "order_1",
            "tracking_url": "https://example.com/track",
            "account_email": "secret@example.com",
            "account_password": "secret-password",
            "cart": {
                "store_name": "Chipotle",
                "client_total_cents": 1650,
                "items": [{"name": "Bowl", "quantity": 2}],
            },
        }
        payload = success_webhook_payload(session, job, "OVIO DD", phrase="ordered and locked in")
        rendered = str(payload)
        self.assertIn("Order Placed", rendered)
        self.assertIn("ordered and locked in", rendered)
        self.assertIn("$16.50", rendered)
        self.assertIn("Chipotle", rendered)
        self.assertIn("<@123>", rendered)
        self.assertNotIn("$3.50", rendered)
        self.assertNotIn("tracking", rendered.lower())
        self.assertNotIn("order_1", rendered)
        self.assertNotIn("secret@example.com", rendered)
        self.assertNotIn("secret-password", rendered)
        self.assertNotIn("1111", rendered)
        self.assertEqual(payload["embeds"][0]["color"], G6_SUCCESS)
        self.assertEqual([field["name"] for field in payload["embeds"][0]["fields"]], ["User", "Total", "Store"])
        self.assertEqual(payload["allowed_mentions"], {"users": ["123"]})

    def test_success_payload_keeps_draft_store_when_settlement_omits_cart(self):
        session = OrderSession(user_id=123, store_name="Raising Cane's")
        job = {"payment_status": "succeeded", "cart": {"client_total_cents": 2140}}

        payload = success_webhook_payload(session, job, "OVIO DD", phrase="ordered up")

        self.assertEqual(payload["embeds"][0]["description"], "🏪 **Raising Cane's**")
        self.assertEqual(payload["embeds"][0]["fields"][2]["value"], "Raising Cane's")

    def test_webhook_url_preserves_thread_and_waits(self):
        url = webhook_url_with_wait("https://discord.com/api/webhooks/1/token?thread_id=2")
        self.assertIn("thread_id=2", url)
        self.assertIn("wait=true", url)

    def test_tracker_uses_encrypted_slug_instead_of_order_id(self):
        order_id = "d8172e5d-ed0c-417f-823e-ad255ff28132"
        url = order_tracker_url(
            {"order_uuid": order_id},
            slug_secret=TEST_SLUG_SECRET,
            base_url="https://food-order-tracker.up.railway.app/",
        )
        slug = parse_qs(urlsplit(url).query)["order"][0]

        self.assertTrue(slug.startswith("v1."))
        self.assertNotIn(order_id, url)
        payload = base64.urlsafe_b64decode(slug.removeprefix("v1.") + "==")
        plaintext = AESGCM(bytes(range(32))).decrypt(payload[:12], payload[12:], SLUG_AAD)
        self.assertEqual(plaintext.decode(), order_id)

    def test_tracking_configuration_rejects_missing_or_invalid_keys(self):
        with self.assertRaises(RuntimeError):
            configure_tracking("https://tracker.example/", "")
        with self.assertRaises(RuntimeError):
            configure_tracking("https://tracker.example/", "not-a-32-byte-key")


if __name__ == "__main__":
    unittest.main()
