import re
import ast


def validate_strategy_code(code: str) -> dict:
    """Validate generated strategy code for syntax and basic safety."""
    errors = []

    # Check syntax
    try:
        ast.parse(code)
    except SyntaxError as e:
        errors.append(f"Syntax error at line {e.lineno}: {e.msg}")

    # Check for required class
    if "IStrategy" not in code:
        errors.append("Missing IStrategy base class")

    # Check for dangerous imports/calls
    dangerous = ["os.system", "subprocess", "eval(", "exec(", "__import__"]
    for d in dangerous:
        if d in code:
            errors.append(f"Dangerous pattern detected: {d}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }


def validate_pair(pair: str) -> bool:
    """Validate a trading pair format like BTC/USDT."""
    return bool(re.match(r"^[A-Z0-9]+/[A-Z0-9]+$", pair.upper()))
