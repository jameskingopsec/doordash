import unittest

from src.addressing import normalize_address, parse_address


class AddressNormalizationTests(unittest.TestCase):
    def test_missing_street_city_comma_is_normalized(self):
        self.assertEqual(
            normalize_address("4200 W Billy ct drive Lincoln, NE 68524"),
            "4200 W Billy Ct Dr, Lincoln, NE 68524, USA",
        )

    def test_standard_comma_address_adds_country(self):
        self.assertEqual(
            normalize_address("123 Main St, Chicago, IL 60601"),
            "123 Main St, Chicago, IL 60601, USA",
        )

    def test_full_state_and_no_commas_are_supported(self):
        self.assertEqual(
            normalize_address("2678 Sawgrass St El Cajon California 92019"),
            "2678 Sawgrass St, El Cajon, CA 92019, USA",
        )

    def test_pipe_and_newline_forms_are_supported(self):
        self.assertEqual(
            normalize_address("524 31st St|Union City|New Jersey|07087"),
            "524 31st St, Union City, NJ 07087, USA",
        )

    def test_extra_commas_and_omitted_state_are_supported(self):
        cases = {
            "123 Main St, Chicago, IL, 60601": "123 Main St, Chicago, IL 60601, USA",
            "123 Main St, Chicago IL 60601": "123 Main St, Chicago, IL 60601, USA",
            "1474 Summer Street, Hammond, 46320": "1474 Summer St, Hammond, IN 46320, USA",
            "123 Main St, Apt 4, Chicago, IL 60601": "123 Main St, Apt 4, Chicago, IL 60601, USA",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_address(value), expected)
        self.assertEqual(
            normalize_address("524 31st St\nUnion City\nNJ 07087\nUSA"),
            "524 31st St, Union City, NJ 07087, USA",
        )

    def test_unparseable_input_is_preserved(self):
        self.assertEqual(normalize_address("Central Park"), "Central Park")
        self.assertEqual(parse_address("")["street"], "")


if __name__ == "__main__":
    unittest.main()
