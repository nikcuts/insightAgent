import unittest

from slugify import slugify


class SlugifyTests(unittest.TestCase):
    def test_trims_outer_whitespace(self) -> None:
        self.assertEqual(slugify("  Hello World  "), "hello-world")

    def test_collapses_inner_whitespace(self) -> None:
        self.assertEqual(slugify("A   B"), "a-b")


if __name__ == "__main__":
    unittest.main()
