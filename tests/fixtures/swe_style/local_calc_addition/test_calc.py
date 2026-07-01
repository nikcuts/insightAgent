import unittest

from calc import calculate


class CalculatorTests(unittest.TestCase):
    def test_addition(self) -> None:
        self.assertEqual(calculate(2, "+", 3), 5)

    def test_subtraction(self) -> None:
        self.assertEqual(calculate(7, "-", 4), 3)

    def test_multiplication(self) -> None:
        self.assertEqual(calculate(6, "*", 7), 42)


if __name__ == "__main__":
    unittest.main()

