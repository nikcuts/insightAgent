import re


def slugify(value: str) -> str:
    return re.sub(r"\s+", "-", value).lower()
