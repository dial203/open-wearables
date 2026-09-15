def clamp(value, low, high):
    """Return value bounded to [low, high]."""
    return max(low, min(value, high))


def safe_divide(a, b):
    if b == 0:
        return None
    return a / b
