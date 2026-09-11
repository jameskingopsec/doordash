import json
from pathlib import Path
import tempfile
import unittest

from src.models import FlowState, OrderSession
from src.store import SessionStore


class StoreTests(unittest.TestCase):
    def test_round_trip_excludes_card_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            store = SessionStore(path)
            original = OrderSession(
                user_id=7,
                job_id="job_123",
                card_last4="1111",
                card={"number": "4111111111111111", "cvv": "123"},
                state=FlowState.DRAFT_READY,
            )
            store.save([original])
            raw = path.read_text()
            self.assertNotIn("4111111111111111", raw)
            self.assertNotIn('"cvv"', raw)
            restored = store.load()[7]
            self.assertEqual(restored.job_id, "job_123")
            self.assertEqual(restored.state, FlowState.DRAFT_READY)
            self.assertIsNone(restored.card)


if __name__ == "__main__":
    unittest.main()

