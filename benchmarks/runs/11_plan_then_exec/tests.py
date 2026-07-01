import unittest
from converter import *

class TestConverter(unittest.TestCase):

    def test_meters_to_centimeters(self):
        self.assertAlmostEqual(a, b, places=4)meters_to_centimeters(1), 100)
        self.assertAlmostEqual(a, b, places=4)meters_to_centimeters(2.5), 250)

    def test_meters_to_feet(self):
        self.assertAlmostEqual(a, b, places=4)meters_to_feet(1), 3.28084)
        self.assertAlmostEqual(a, b, places=4)meters_to_feet(2.5), 8.2021)

    def test_meters_to_inches(self):
        self.assertAlmostEqual(a, b, places=4)meters_to_inches(1), 39.3701)
        self.assertAlmostEqual(a, b, places=4)meters_to_inches(2.5), 98.42525)

    def test_centimeters_to_meters(self):
        self.assertAlmostEqual(a, b, places=4)centimeters_to_meters(100), 1)
        self.assertAlmostEqual(a, b, places=4)centimeters_to_meters(250), 2.5)

    def test_centimeters_to_feet(self):
        self.assertAlmostEqual(a, b, places=4)centimeters_to_feet(100), 3.28084)
        self.assertAlmostEqual(a, b, places=4)centimeters_to_feet(250), 8.2021)

    def test_centimeters_to_inches(self):
        self.assertAlmostEqual(a, b, places=4)centimeters_to_inches(100), 39.3701)
        self.assertAlmostEqual(a, b, places=4)centimeters_to_inches(250), 98.42525)

    def test_feet_to_meters(self):
        self.assertAlmostEqual(a, b, places=4)feet_to_meters(3.28084), 1)
        self.assertAlmostEqual(a, b, places=4)feet_to_meters(8.2021), 2.5)

    def test_feet_to_centimeters(self):
        self.assertAlmostEqual(a, b, places=4)feet_to_centimeters(3.28084), 100)
        self.assertAlmostEqual(a, b, places=4)feet_to_centimeters(8.2021), 250)

    def test_feet_to_inches(self):
        self.assertAlmostEqual(a, b, places=4)feet_to_inches(3.28084), 39.3701)
        self.assertAlmostEqual(a, b, places=4)feet_to_inches(8.2021), 98.4252)

    def test_inches_to_meters(self):
        self.assertAlmostEqual(a, b, places=4)inches_to_meters(39.3701), 1)
        self.assertAlmostEqual(a, b, places=4)inches_to_meters(98.42525), 2.5)

    def test_inches_to_centimeters(self):
        self.assertAlmostEqual(a, b, places=4)inches_to_centimeters(39.3701), 100)
        self.assertAlmostEqual(a, b, places=4)inches_to_centimeters(98.42525), 250)

    def test_inches_to_feet(self):
        self.assertAlmostEqual(a, b, places=4)inches_to_feet(39.3701), 3.28084)
        self.assertAlmostEqual(a, b, places=4)inches_to_feet(98.4252), 8.2021)

if __name__ == '__main__':
    unittest.main()