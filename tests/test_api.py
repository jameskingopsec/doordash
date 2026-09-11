import json
from unittest.mock import AsyncMock
import unittest

from src.api import WoolixApiClient, WoolixApiError
from src.bot import SETUP_CONNECTION_ERROR, api_error_panel, is_credit_exhausted


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def text(self):
        return json.dumps(self.payload)


class FakeRequestContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    def __init__(self):
        self.closed = False
        self.requests = []

    def request(self, method, url, json=None):
        path = "/" + url.split("/", 3)[-1] if "://" in url else url
        self.requests.append({"method": method, "path": path, "body": json})
        if path.endswith("/missing"):
            return FakeRequestContext(FakeResponse(404, {"error": "not found", "code": "missing"}))
        if method == "POST" and path == "/api/v1/jobs":
            return FakeRequestContext(FakeResponse(202, {"job_id": "job_1", "status": "pending"}))
        return FakeRequestContext(FakeResponse(200, {"job_id": "job_1", "status": "draft_ready", "cart": {}}))

    async def close(self):
        self.closed = True


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = WoolixApiClient("https://woolix.test", "dda_test", 5)
        self.fake_session = FakeSession()
        self.client._session = self.fake_session

    async def asyncTearDown(self):
        await self.client.close()

    async def test_documented_job_paths(self):
        await self.client.create_job({"group_cart": "cart link"})
        await self.client.get_job("job_1")
        await self.client.configure_job("job_1", {"tip_cents": 300})
        await self.client.proceed_job("job_1")
        await self.client.cancel_job("job_1")
        await self.client.set_dropoff("job_1", "leave")
        await self.client.set_fulfillment("job_1", "pickup")
        await self.client.rebuild_job("job_1", "new cart")

        self.assertEqual(
            [(request["method"], request["path"]) for request in self.fake_session.requests],
            [
                ("POST", "/api/v1/jobs"),
                ("GET", "/api/v1/jobs/job_1"),
                ("POST", "/api/v1/jobs/job_1/configure"),
                ("POST", "/api/v1/jobs/job_1/proceed"),
                ("POST", "/api/v1/jobs/job_1/cancel"),
                ("POST", "/api/v1/jobs/job_1/dropoff"),
                ("POST", "/api/v1/jobs/job_1/fulfillment"),
                ("POST", "/api/v1/jobs/job_1/rebuild"),
            ],
        )
        self.assertEqual(self.fake_session.requests[2]["body"], {"tip_cents": 300})

    async def test_errors_preserve_status_code_and_machine_code(self):
        with self.assertRaises(WoolixApiError) as raised:
            await self.client.get_job("missing")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.code, "missing")
        self.assertEqual(str(raised.exception), "not found")

    async def test_credit_exhaustion_is_recognized_for_ui(self):
        self.assertTrue(is_credit_exhausted(WoolixApiError(402, {"error": "payment required"})))
        self.assertTrue(is_credit_exhausted({"error": {"code": "insufficient_credits"}}))
        self.assertFalse(is_credit_exhausted(WoolixApiError(400, {"error": "invalid card"})))

        panel = api_error_panel("Could Not Prepare Order", WoolixApiError(402, {"error": "no credits"}))
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        self.assertIn(SETUP_CONNECTION_ERROR, rendered)
        self.assertNotIn("credit", rendered.lower())
        self.assertNotIn("wool", rendered.lower())

    async def test_api_details_are_hidden_from_customer_error_panel(self):
        panel = api_error_panel(
            "Could Not Prepare Order",
            WoolixApiError(500, {"error": "API job_id failed", "code": "internal_123"}),
        )
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        self.assertNotIn("API", rendered)
        self.assertNotIn("job_id", rendered)
        self.assertNotIn("internal_123", rendered)

    async def test_draft_polling_emits_only_changed_states(self):
        client = WoolixApiClient("https://example.invalid", "key", 5)
        client.get_job = AsyncMock(side_effect=[
            {"status": "validating", "substatus": "Checking cart"},
            {"status": "validating", "substatus": "Checking cart"},
            {"status": "draft_ready", "substatus": "Ready", "cart": {}},
        ])
        updates = []

        async def on_update(job):
            updates.append((job["status"], job["substatus"]))

        result = await client.wait_for_draft(
            "job_1",
            timeout_seconds=1,
            interval_seconds=0.001,
            on_update=on_update,
        )
        self.assertEqual(result["status"], "draft_ready")
        self.assertEqual(updates, [("validating", "Checking cart"), ("draft_ready", "Ready")])

    async def test_awaiting_config_keeps_polling_in_main_flow(self):
        client = WoolixApiClient("https://example.invalid", "key", 5)
        client.get_job = AsyncMock(side_effect=[
            {"status": "awaiting_config", "substatus": "Adding card"},
            {"status": "draft_ready", "card_error": {"message": "Your card was declined."}},
        ])
        result = await client.wait_for_draft(
            "job_1",
            timeout_seconds=1,
            interval_seconds=0.001,
        )
        self.assertEqual(result["status"], "draft_ready")
        self.assertEqual(client.get_job.await_count, 2)

    async def test_cardless_draft_stops_at_draft_view(self):
        client = WoolixApiClient("https://example.invalid", "key", 5)
        client.get_job = AsyncMock(return_value={"status": "awaiting_config", "cart": {"items": []}})
        result = await client.wait_for_draft(
            "job_1",
            timeout_seconds=1,
            interval_seconds=0.001,
            awaiting_config_done=True,
        )
        self.assertEqual(result["status"], "awaiting_config")
        self.assertEqual(client.get_job.await_count, 1)


if __name__ == "__main__":
    unittest.main()
