import json
import os


class KVStore:
    def __init__(self, path):
        self.path = path
        if not os.path.exists(self.path):
            with open(self.path, 'w') as f:
                json.dump({}, f)

    def set(self, key, value):
        with open(self.path, 'r+') as f:
            data = json.load(f) if f.read(1) else {}
            f.seek(0)
            data[key] = value
            f.seek(0)
            json.dump(data, f, indent=4)
            f.truncate()

    def get(self, key):
        if not os.path.exists(self.path):
            return None
        with open(self.path, 'r') as f:
            data = json.load(f) if f.read(1) else {}
            f.seek(0)
            return data.get(key)

    def delete(self, key):
        if not os.path.exists(self.path):
            return
        with open(self.path, 'r+') as f:
            data = json.load(f) if f.read(1) else {}
            f.seek(0)
            if key in data:
                del data[key]
            f.seek(0)
            json.dump(data, f, indent=4)
            f.truncate()

    def list_keys(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, 'r') as f:
            data = json.load(f) if f.read(1) else {}
            f.seek(0)
            return list(data.keys())