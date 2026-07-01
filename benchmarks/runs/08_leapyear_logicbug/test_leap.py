import unittest

from leap import is_leap


class LeapTests(unittest.TestCase):
    def test_div400(self):
        self.assertTrue(is_leap(2000))

    def test_div100_not400(self):
        self.assertFalse(is_leap(1900))
        self.assertFalse(is_leap(2100))

    def test_div4(self):
        self.assertTrue(is_leap(2004))

    def test_common(self):
        self.assertFalse(is_leap(2023))


if __name__ == "__main__":
    unittest.main()
