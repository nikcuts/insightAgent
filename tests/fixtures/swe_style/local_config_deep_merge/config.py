def merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    result = dict(base)
    result.update(override)
    return result
