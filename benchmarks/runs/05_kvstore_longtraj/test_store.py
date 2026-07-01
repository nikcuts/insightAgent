import os
import tempfile
import unittest

from kvstore.store import KVStore


class KVStoreTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        handle.close()
        self.path = handle.name

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_set_get(self):
        store = KVStore(self.path)
        store.set("a", "1")
        self.assertEqual(store.get("a"), "1")

    def test_missing_key_returns_none(self):
        self.assertIsNone(KVStore(self.path).get("nope"))

    def test_delete(self):
        store = KVStore(self.path)
        store.set("a", "1")
        store.delete("a")
        self.assertIsNone(store.get("a"))

    def test_empty_list_keys(self):
        self.assertEqual(KVStore(self.path).list_keys(), [])

    def test_persistence_across_instances(self):
        KVStore(self.path).set("x", "9")
        self.assertEqual(KVStore(self.path).get("x"), "9")


if __name__ == "__main__":
    unittest.main()
