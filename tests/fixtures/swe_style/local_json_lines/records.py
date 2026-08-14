import json


def load_records(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines()]
