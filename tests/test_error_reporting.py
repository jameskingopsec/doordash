import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.api import WoolixApiError
from src.bot import WoolixBot
from src.error_reporting import diagnostic_dump, event_log_message


class ErrorReportingTests(unittest.IsolatedAsyncioTestCase):
    def test_diagnostic_dump_redacts_secrets_but_keeps_error_details(self):
        error = WoolixApiError(400, {
            "error": "card_declined",
            "card_error": {"reason": "do_not_honor"},
            "card": {"number": "4111111111111111", "cvv": "123"},
            "account_password": "super-secret",
            "api_key": "dda_example_key",
            "webhook_url": "https://discord.com/api/webhooks/123/private-token",
        })
        rendered = diagnostic_dump(error)
        self.assertIn("card_declined", rendered)
        self.assertIn("do_not_honor", rendered)
        self.assertNotIn("4111111111111111", rendered)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("dda_example_key", rendered)
        self.assertNotIn("private-token", rendered)

    async def test_channel_report_is_deduplicated_and_has_attachment(self):
        channel = SimpleNamespace(send=AsyncMock())
        fake_bot = SimpleNamespace(
            settings=SimpleNamespace(log_channel_id=1519864975164965001),
            reported_error_fingerprints=set(),
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(),
        )
        error = WoolixApiError(500, {"error": "upstream failure", "request_id": "req_123"})
        await WoolixBot.report_error(fake_bot, "Create priced draft", error, user_id=123, job_id="job_1")
        await WoolixBot.report_error(fake_bot, "Create priced draft", error, user_id=123, job_id="job_1")

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIn("Create priced draft", kwargs["content"])
        self.assertIn("job_1", kwargs["content"])
        self.assertTrue(kwargs["file"].filename.startswith("ovio-error-"))
        kwargs["file"].close()

    def test_activity_log_redacts_secrets_but_keeps_identifiers(self):
        rendered = event_log_message(
            "Checkout completed",
            level="success",
            user_id=664560958672207914,
            job_id="job_123",
            details={
                "admin_user_id": 570208906333257745,
                "card": {"number": "4111111111111111", "cvv": "123"},
                "api_key": "dda_example_key",
                "store": "Example Store",
            },
            timestamp="2026-08-23 12:00:00 UTC",
        )
        self.assertIn("Checkout completed", rendered)
        self.assertIn("664560958672207914", rendered)
        self.assertIn("570208906333257745", rendered)
        self.assertIn("Example Store", rendered)
        self.assertNotIn("4111111111111111", rendered)
        self.assertNotIn("dda_example_key", rendered)

    async def test_activity_event_is_sent_to_configured_channel(self):
        channel = SimpleNamespace(send=AsyncMock())
        fake_bot = SimpleNamespace(
            settings=SimpleNamespace(log_channel_id=1519864975164965001),
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(),
        )

        await WoolixBot.report_event(
            fake_bot,
            "Bot online",
            level="success",
            details={"restored_sessions": 3},
        )

        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIn("Bot online", kwargs["content"])
        self.assertIn("restored_sessions", kwargs["content"])


if __name__ == "__main__":
    unittest.main()
