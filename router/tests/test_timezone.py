"""Tests for router.timezone — the free-form TZ resolver."""
from __future__ import annotations

import unittest

from router.timezone import resolve


class TestIANAPassthrough(unittest.TestCase):
    def test_full_iana_name(self):
        self.assertEqual(resolve("America/Los_Angeles"), "America/Los_Angeles")
        self.assertEqual(resolve("Asia/Kolkata"), "Asia/Kolkata")
        self.assertEqual(resolve("Europe/London"), "Europe/London")

    def test_iana_with_whitespace(self):
        self.assertEqual(resolve("  America/New_York  "), "America/New_York")

    def test_utc_passthrough(self):
        self.assertEqual(resolve("UTC"), "UTC")


class TestAbbreviations(unittest.TestCase):
    def test_us_zones(self):
        self.assertEqual(resolve("PST"), "America/Los_Angeles")
        self.assertEqual(resolve("PDT"), "America/Los_Angeles")
        self.assertEqual(resolve("PT"), "America/Los_Angeles")
        self.assertEqual(resolve("ET"), "America/New_York")
        self.assertEqual(resolve("EST"), "America/New_York")
        self.assertEqual(resolve("CT"), "America/Chicago")
        self.assertEqual(resolve("MT"), "America/Denver")

    def test_case_insensitive(self):
        self.assertEqual(resolve("pst"), "America/Los_Angeles")
        self.assertEqual(resolve("Pst"), "America/Los_Angeles")
        self.assertEqual(resolve("pST"), "America/Los_Angeles")

    def test_india_ist(self):
        # IST defaults to India in our table — the more common case
        self.assertEqual(resolve("IST"), "Asia/Kolkata")

    def test_uk_zones(self):
        self.assertEqual(resolve("GMT"), "Europe/London")
        self.assertEqual(resolve("BST"), "Europe/London")

    def test_asia_pacific(self):
        self.assertEqual(resolve("JST"), "Asia/Tokyo")
        self.assertEqual(resolve("KST"), "Asia/Seoul")
        self.assertEqual(resolve("SGT"), "Asia/Singapore")
        self.assertEqual(resolve("AEDT"), "Australia/Sydney")
        self.assertEqual(resolve("NZST"), "Pacific/Auckland")


class TestCities(unittest.TestCase):
    def test_us_cities(self):
        self.assertEqual(resolve("Los Angeles"), "America/Los_Angeles")
        self.assertEqual(resolve("NYC"), "America/New_York")
        self.assertEqual(resolve("nyc"), "America/New_York")
        self.assertEqual(resolve("SF"), "America/Los_Angeles")
        self.assertEqual(resolve("Chicago"), "America/Chicago")
        self.assertEqual(resolve("Honolulu"), "Pacific/Honolulu")

    def test_india_cities(self):
        self.assertEqual(resolve("Mumbai"), "Asia/Kolkata")
        self.assertEqual(resolve("Bangalore"), "Asia/Kolkata")
        self.assertEqual(resolve("Bengaluru"), "Asia/Kolkata")
        self.assertEqual(resolve("Delhi"), "Asia/Kolkata")

    def test_europe_cities(self):
        self.assertEqual(resolve("London"), "Europe/London")
        self.assertEqual(resolve("Paris"), "Europe/Paris")
        self.assertEqual(resolve("Berlin"), "Europe/Berlin")
        self.assertEqual(resolve("Amsterdam"), "Europe/Amsterdam")

    def test_strip_prefix_phrases(self):
        self.assertEqual(resolve("I'm in NYC"), "America/New_York")
        self.assertEqual(resolve("based in San Francisco"), "America/Los_Angeles")
        self.assertEqual(resolve("we live in London"), "Europe/London")

    def test_strip_country_suffix(self):
        self.assertEqual(resolve("Boston, USA"), "America/New_York")
        self.assertEqual(resolve("Mumbai, India"), "Asia/Kolkata")
        self.assertEqual(resolve("London UK"), "Europe/London")

    def test_punctuation_tolerated(self):
        self.assertEqual(resolve("Tokyo."), "Asia/Tokyo")
        self.assertEqual(resolve("Tokyo!"), "Asia/Tokyo")


class TestUTCOffset(unittest.TestCase):
    def test_zero_offset(self):
        self.assertEqual(resolve("UTC+0"), "UTC")
        self.assertEqual(resolve("UTC-0"), "UTC")
        self.assertEqual(resolve("+0"), "UTC")

    def test_negative_offset(self):
        self.assertEqual(resolve("UTC-5"), "Etc/GMT+5")
        self.assertEqual(resolve("GMT-8"), "Etc/GMT+8")

    def test_positive_offset(self):
        self.assertEqual(resolve("UTC+1"), "Etc/GMT-1")
        self.assertEqual(resolve("GMT+9"), "Etc/GMT-9")

    def test_fractional_offset_unsupported(self):
        # India is UTC+5:30 — Etc/GMT zones can't represent the :30
        # User should use "Asia/Kolkata" or "IST" instead.
        self.assertIsNone(resolve("UTC+5:30"))

    def test_offset_out_of_range(self):
        # Etc/GMT only goes to ±14
        self.assertIsNone(resolve("UTC+20"))


class TestUnresolvable(unittest.TestCase):
    def test_garbage_returns_none(self):
        self.assertIsNone(resolve("the moon"))
        self.assertIsNone(resolve("zzzzz"))
        self.assertIsNone(resolve("123456"))

    def test_empty_returns_none(self):
        self.assertIsNone(resolve(""))
        self.assertIsNone(resolve("   "))
        self.assertIsNone(resolve(None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
