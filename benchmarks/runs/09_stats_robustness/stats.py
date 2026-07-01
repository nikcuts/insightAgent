def mean(values):
    return sum(values) / len(values)


def median(values):
    s = sorted(values)
    return s[len(s) // 2]
