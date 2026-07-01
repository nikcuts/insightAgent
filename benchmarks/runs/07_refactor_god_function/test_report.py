import unittest

from report import build_report


class ReportTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(build_report(["1", "2", "3"]), "count=3 total=6 avg=2.0")

    def test_empty(self):
        self.assertEqual(build_report([]), "count=0 total=0 avg=0")


if __name__ == "__main__":
    unittest.main()
