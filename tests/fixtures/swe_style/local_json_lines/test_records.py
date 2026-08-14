import unittest

from records import load_records


class JsonLinesTests(unittest.TestCase):
    def test_ignores_blank_lines(self) -> None:
        payload = '{"id": 1}\n\n  \n{"id": 2}\n'

        self.assertEqual(load_records(payload), [{"id": 1}, {"id": 2}])


if __name__ == "__main__":
    unittest.main()
