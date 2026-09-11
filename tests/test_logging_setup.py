import logging
import unittest

from src.logging_setup import LOG_FORMAT, RedactingFormatter


class LoggingSetupTests(unittest.TestCase):
    def test_formatter_redacts_credentials_and_card_numbers(self):
        record = logging.LogRecord(
            name="ovio.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=12,
            msg=(
                "token AAAABBBBCCCCDDDDEEEE.FFFFF."
                "GGGGHHHHIIIIJJJJKKKKLLLLMMMM and card 4111111111111111 "
                "and key dda_example_key"
            ),
            args=(),
            exc_info=None,
        )
        rendered = RedactingFormatter(LOG_FORMAT).format(record)
        self.assertNotIn("4111111111111111", rendered)
        self.assertNotIn("dda_example_key", rendered)
        self.assertNotIn("GGGGHHHHIIIIJJJJKKKKLLLLMMMM", rendered)
        self.assertIn("[CARD REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
