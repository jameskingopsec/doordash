import unittest

from src.models import OrderSession, money, parse_card, parse_card_profile, parse_expiry


class ModelTests(unittest.TestCase):
    def test_parse_card_normalizes_fields(self):
        card = parse_card("4111 1111 1111 1111", "6/31", "1 2 3", "90250")
        self.assertEqual(card, {
            "number": "4111111111111111",
            "exp_month": "06",
            "exp_year": "31",
            "cvv": "123",
            "zip": "90250",
        })

    def test_parse_expiry_accepts_four_digit_year(self):
        self.assertEqual(parse_expiry("12/2030"), ("12", "30"))

    def test_parse_combined_card_profile(self):
        card = parse_card_profile("4111111111111111 | 06/31 | 123 | 90250")
        self.assertEqual(card["number"], "4111111111111111")
        self.assertEqual(card["exp_month"], "06")
        self.assertEqual(card["zip"], "90250")

    def test_parse_card_profile_accepts_api_json(self):
        card = parse_card_profile(
            '{"number":"4111111111111111","exp_month":"06","exp_year":"31","cvv":"123","zip":"90250"}'
        )
        self.assertEqual(card["exp_year"], "31")

    def test_safe_session_never_persists_card(self):
        session = OrderSession(
            user_id=123,
            store_name="Chipotle",
            card={"number": "4111111111111111", "cvv": "123"},
        )
        data = session.safe_dict()
        self.assertNotIn("card", data)
        self.assertNotIn("4111111111111111", str(data))
        self.assertNotIn("123", str(data).replace("123", "", 1))
        self.assertEqual(data["store_name"], "Chipotle")

    def test_create_payload_matches_woolix_contract(self):
        session = OrderSession(
            user_id=1,
            fulfillment="delivery",
            group_cart="cart link",
            address="123 Main St",
            unit="Apt 4",
            name="Jane Doe",
            delivery_note="Leave at door",
            promo="SAVE30",
            tip_cents=300,
            card={"number": "4111111111111111", "exp_month": "06", "exp_year": "31", "cvv": "123", "zip": "90250"},
        )
        payload = session.create_payload()
        self.assertEqual(payload["group_cart"], "cart link")
        self.assertEqual(payload["tip"], 300)
        self.assertNotIn("fulfillment", payload)
        self.assertEqual(payload["card"]["exp_year"], "31")

    def test_create_payload_one_shot_has_only_documented_inputs(self):
        card = parse_card_profile("4111111111111111|06/31|123|90250")
        session = OrderSession(user_id=1, group_cart="cart link", address="123 Main St", card=card)
        self.assertEqual(
            session.create_payload(),
            {"group_cart": "cart link", "address": "123 Main St", "card": card},
        )

    def test_money_uses_integer_cents(self):
        self.assertEqual(money(1650), "$16.50")


if __name__ == "__main__":
    unittest.main()
