def calculate(left: int, operator: str, right: int) -> int:
    if operator == "+":
        return left - right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    raise ValueError(f"unsupported operator: {operator}")

