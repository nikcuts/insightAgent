import unittest

from stats import mean, median


class StatsTests(unittest.TestCase):
    def test_empty_mean_none(self):
        self.assertIsNone(mean([]))

    def test_empty_median_none(self):
        self.assertIsNone(median([]))

    def test_single(self):
        self.assertEqual(mean([5]), 5)
        self.assertEqual(median([5]), 5)

    def test_mean_multi(self):
        self.assertEqual(mean([1, 2, 3, 4]), 2.5)

    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)


if __name__ == "__main__":
    unittest.main()
