import unittest

from config import merge


class ConfigMergeTests(unittest.TestCase):
    def test_preserves_unrelated_nested_defaults(self) -> None:
        base = {"service": {"host": "localhost", "timeout": 30}, "debug": False}
        override = {"service": {"timeout": 5}}

        self.assertEqual(
            merge(base, override),
            {"service": {"host": "localhost", "timeout": 5}, "debug": False},
        )


if __name__ == "__main__":
    unittest.main()
