import unittest

from src.formatting import (
    customer_block,
    error_message,
    friendly_error_details,
    item_lines,
    price_lines,
    suspected_duplicate_item_cents,
)
from src.models import OrderSession


JOB = {
    "status": "draft_ready",
    "input": {"tip": 300},
    "cart": {
        "store_name": "Chipotle",
        "subtotal_cents": 2000,
        "promo_discount_cents": 1000,
        "tax_and_fees_cents": 350,
        "client_total_cents": 1650,
        "items": [{"name": "Burrito Bowl", "quantity": 2, "unit_price_cents": 1000}],
    },
}


class FormattingTests(unittest.TestCase):
    def test_customer_block_matches_panel_language(self):
        session = OrderSession(user_id=1, name="Jane Doe", address="123 Main St", unit="Apt 4")
        rendered = customer_block(session)
        self.assertIn("Jane Doe", rendered)
        self.assertIn("123 Main St, Apt 4", rendered)
        self.assertIn("Delivery", rendered)

    def test_cart_and_price_lines_render_integer_cents(self):
        self.assertIn("**2×** Burrito Bowl — $20.00", item_lines(JOB))
        rendered = price_lines(JOB)
        self.assertIn("Promotion: **-$10.00**", rendered)
        self.assertIn("### Total: $16.50", rendered)

    def test_hidden_duplicate_item_is_detected_from_exact_subtotal_difference(self):
        job = {
            "cart": {
                "subtotal_cents": 7197,
                "items": [
                    {"name": "Flavor Lineup Wings", "quantity": 1, "unit_price_cents": 2999},
                    {"name": "3 pc Crispy Tender Combo", "quantity": 1, "unit_price_cents": 1199},
                ],
            }
        }
        self.assertEqual(suspected_duplicate_item_cents(job), 2999)

    def test_arbitrary_modifier_difference_is_not_marked_as_duplicate(self):
        job = {
            "cart": {
                "subtotal_cents": 3330,
                "items": [
                    {"name": "Burrito Bowl", "quantity": 1, "unit_price_cents": 1400},
                    {"name": "Burrito Bowl", "quantity": 1, "unit_price_cents": 1710},
                ],
            }
        }
        self.assertIsNone(suspected_duplicate_item_cents(job))

    def test_error_precedence_matches_actionable_api_fields(self):
        job = {
            "error": {"message": "generic"},
            "place_order_error": {"message": "store closed"},
            "card_error": {"message": "card declined"},
        }
        self.assertEqual(
            error_message(job),
            "Your card was declined. Add a different card or contact your card issuer.",
        )

    def test_raw_decline_response_is_reduced_to_customer_copy(self):
        job = {
            "card_error": {
                "message": 'add_card → 400: {"response":"{\\"error_code\\":\\"card_declined\\",\\"grpcStatus\\":\\"INVALID_ARGUMENT\\"}"}'
            }
        }
        self.assertEqual(
            error_message(job),
            "Your card was declined. Add a different card or contact your card issuer.",
        )

    def test_payment_status_decline_never_falls_back_to_generic_checkout_error(self):
        title, message = friendly_error_details({"status": "failed", "payment_status": "declined"})
        self.assertEqual(title, "Payment Declined")
        self.assertIn("card issuer declined", message)
        self.assertNotIn("fresh group cart", message)

        title, message = friendly_error_details({"status": "failed", "payment_status": "failed"})
        self.assertEqual(title, "Payment Failed")
        self.assertIn("different card", message)

    def test_specific_order_failure_takes_priority_over_generic_payment_failure(self):
        title, message = friendly_error_details({
            "status": "failed",
            "payment_status": "failed",
            "error": {"message": "store closed"},
        })
        self.assertEqual(title, "Store Unavailable")
        self.assertIn("another store", message)

    def test_payment_decline_reasons_are_shown_as_actionable_messages(self):
        cases = (
            ("insufficient_funds", "Payment Declined", "insufficient funds"),
            ("incorrect_cvc", "Check Your Card", "security code"),
            ("incorrect_zip", "Check Your Card", "billing ZIP"),
            ("expired_card", "Card Expired", "expired"),
            ("card_not_supported", "Card Not Supported", "different card"),
            ("do_not_honor", "Payment Declined", "issuer declined"),
            ("processing_error", "Payment Processing Error", "right now"),
        )
        for reason, expected_title, expected_message in cases:
            with self.subTest(reason=reason):
                title, message = friendly_error_details({
                    "status": "failed",
                    "payment_status": "declined",
                    "payment_error": {"decline_reason": reason},
                })
                self.assertEqual(title, expected_title)
                self.assertIn(expected_message, message)

    def test_failures_are_mapped_to_actionable_customer_messages(self):
        cases = (
            ({"error": {"message": "group cart already used"}}, "Fresh Cart Needed", "fresh group cart"),
            ({"error": {"message": "invalid address"}}, "Check Your Address", "full address"),
            ({"error": {"type": "address_not_found", "message": "provider detail"}}, "Check Your Address", "full address"),
            ({"error": {"message": "store closed"}}, "Store Unavailable", "another store"),
            ({"error": {"message": "item unavailable"}}, "Update Your Cart", "Remove or replace"),
            ({"status": "expired"}, "Draft Expired", "!start"),
        )
        for value, expected_title, expected_message in cases:
            with self.subTest(value=value):
                title, message = friendly_error_details(value)
                self.assertEqual(title, expected_title)
                self.assertIn(expected_message, message)

    def test_unknown_technical_failure_never_exposes_raw_details(self):
        raw = {"error": {"message": "API grpc job_id request failed", "request_id": "private_123"}}
        title, message = friendly_error_details(raw)
        rendered = f"{title} {message}"
        self.assertIn("fresh group cart", rendered)
        self.assertNotIn("grpc", rendered)
        self.assertNotIn("job_id", rendered)
        self.assertNotIn("private_123", rendered)


if __name__ == "__main__":
    unittest.main()
