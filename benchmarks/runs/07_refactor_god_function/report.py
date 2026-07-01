def build_report(numbers):
    data = [int(x) for x in numbers]
    total = 0
    for d in data:
        total += d
    avg = total / len(data) if data else 0
    return f"count={len(data)} total={total} avg={avg}"
