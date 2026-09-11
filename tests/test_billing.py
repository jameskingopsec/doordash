from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from src.billing import BillingStore, CHECKOUT_FEE_CENTS


PACIFIC = ZoneInfo("America/Los_Angeles")


class BillingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = BillingStore(Path(self.temporary.name) / "fees.json")

    def tearDown(self):
        self.temporary.cleanup()

    def test_successful_job_is_charged_once(self):
        self.assertTrue(self.store.record_checkout(123, "job_1"))
        self.assertFalse(self.store.record_checkout(123, "job_1"))
        self.assertEqual(self.store.records[0]["amount_cents"], CHECKOUT_FEE_CENTS)

    def test_balance_summary_totals_only_unpaid_records(self):
        self.store.record_checkout(123, "job-1")
        self.store.record_checkout(123, "job-2")
        self.store.record_checkout(456, "job-3")
        self.store.records[0]["paid_at"] = 1

        summary = self.store.balance_summary(123)

        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["total_cents"], CHECKOUT_FEE_CENTS)
        self.assertEqual(summary["records"][0]["job_id"], "job-2")

    def test_clear_balance_marks_every_unpaid_checkout_paid(self):
        self.store.record_checkout(123, "job-1")
        self.store.record_checkout(123, "job-2")
        self.store.record_checkout(456, "job-3")

        cleared = self.store.clear_balance(123)

        self.assertEqual(cleared["count"], 2)
        self.assertEqual(cleared["total_cents"], CHECKOUT_FEE_CENTS * 2)
        self.assertEqual(self.store.balance_summary(123)["count"], 0)
        self.assertEqual(self.store.balance_summary(456)["count"], 1)

    def test_clear_balance_is_idempotent(self):
        self.store.record_checkout(123, "job-1")
        self.assertEqual(self.store.clear_balance(123)["count"], 1)
        self.assertEqual(self.store.clear_balance(123)["count"], 0)

    def test_nightly_summary_groups_user_fees_and_marks_sent(self):
        created = datetime(2026, 8, 22, 20, 0, tzinfo=PACIFIC)
        self.store.record_checkout(123, "job_1", now=created)
        self.store.record_checkout(123, "job_2", now=created)
        self.assertEqual(self.store.due_summaries(now=created), [])

        nightly = datetime(2026, 8, 22, 23, 0, tzinfo=PACIFIC)
        summaries = self.store.due_summaries(now=nightly)
        self.assertEqual(summaries[0]["count"], 2)
        self.assertEqual(summaries[0]["total_cents"], 700)
        self.store.mark_reminded(summaries[0]["job_ids"], now=nightly)
        self.assertEqual(self.store.due_summaries(now=nightly), [])

        next_morning = datetime(2026, 8, 23, 10, 0, tzinfo=PACIFIC)
        self.assertEqual(self.store.due_summaries(now=next_morning), [])

        next_night = datetime(2026, 8, 23, 23, 0, tzinfo=PACIFIC)
        repeated = self.store.due_summaries(now=next_night)
        self.assertEqual(repeated[0]["count"], 2)

    def test_missed_nightly_window_is_caught_up_after_restart(self):
        created = datetime(2026, 8, 22, 20, 0, tzinfo=PACIFIC)
        self.store.record_checkout(123, "job_1", now=created)

        next_morning = datetime(2026, 8, 23, 10, 0, tzinfo=PACIFIC)
        summaries = self.store.due_summaries(now=next_morning)

        self.assertEqual(summaries[0]["count"], 1)
        self.assertEqual(summaries[0]["total_cents"], 350)

    def test_current_day_checkout_waits_for_tonights_cutoff(self):
        morning = datetime(2026, 8, 23, 9, 0, tzinfo=PACIFIC)
        self.store.record_checkout(123, "job_1", now=morning)

        self.assertEqual(
            self.store.due_summaries(now=datetime(2026, 8, 23, 10, 0, tzinfo=PACIFIC)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
